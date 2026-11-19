"""executor/decision_cycle.py on a paper session (#69).

Real Session + Trade machines (test_session_controller.Day), real decide(); the
feature/bar/news/watchlist sources are injected. Each symbol is set up for the
worked ORB example in tests/brain/test_intraday_decision.py: LTP 1,000, ATR 20,
ORB 995-1,005 -> stop-limit BUY trigger 1,006.00 / limit 1,008.05, 49 shares.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from executor import session_machine as sm
from executor.brokers.feeds.base import Tick
from executor.decision_cycle import MAX_OPEN_TRADES, DecisionCycle
from tests.executor.test_session_controller import Day

IST = ZoneInfo("Asia/Kolkata")
SYMBOLS = ["TCS", "SBIN", "HDFCBANK", "ITC", "LT"]
BATCH = dict(
    atr_14d=20.0,
    overnight_news_sentiment=0.5,
    fo_oi_overnight_change_pct=5.0,
    beta_20d=1.0,
    avg_daily_volume_20d=1e6,
    avg_traded_value_20d=5e9,
)


class _Bars:
    """09:15-09:29 one-minute bars, high 1,005 / low 995, recorded from the open."""

    coverage_start = datetime(2026, 11, 19, 9, 15, tzinfo=IST)

    def bars(self, symbol):
        return [
            SimpleNamespace(
                minute=self.coverage_start + timedelta(minutes=m), high=1005.0, low=995.0
            )
            for m in range(15)
        ]


class Cycle:
    def __init__(self, d: Day, watchlist=SYMBOLS) -> None:
        self.d = d
        self.watchlist = [SimpleNamespace(stock_symbol=s, exchange="NSE") for s in watchlist]
        self.batch_error: Exception | None = None
        db_path = d.h.db.execute("PRAGMA database_list").fetchone()[2]
        self.cycle = DecisionCycle(
            db_path=db_path,
            session=d.ctl,
            supervisor=d.h.sup,
            feed=d.h.feed,
            bar_store=lambda: _Bars(),
            batch_features=self._batch,
            latest_news=lambda sym: d.h.clock.now - timedelta(minutes=30),
            regime=lambda day: "bull_calm",
            risk_config=d.ctl._risk_config,
            ledger=lambda: d.ledger,
            watchlist=lambda day: self.watchlist,
            clock=d.h.clock,
        )

    def _batch(self, sym, exch, day):
        if self.batch_error:
            raise self.batch_error
        return dict(BATCH)

    def quotes(self, ltp: float = 1000.0) -> None:
        for s in SYMBOLS:
            self.d.h.feed.push(
                Tick(
                    symbol=s,
                    exchange="NSE",
                    ltp=ltp,
                    received_at=self.d.h.clock.now,
                    day_open=999.0,
                    prev_close=994.0,
                    avg_traded_price=998.0,
                    best_bid=999.75,
                    best_ask=1000.25,
                )
            )

    def run(self):
        self.quotes()
        return self.cycle.run()


@pytest.fixture()
def c(tmp_path) -> Cycle:
    d = Day(tmp_path)
    d.nifty = 24100.0  # NIFTY up 0.42%: index confirms
    return Cycle(d)


def _trading(c: Cycle) -> None:
    c.d.trading()
    c.d.at(9, 45)


def test_skips_unless_trading(c):
    c.d.at(9, 0)
    assert c.run().skipped == "session_connected"
    assert c.d.h.db.execute("SELECT COUNT(*) FROM intraday_brackets").fetchone()[0] == 0


def test_opens_breakout_entries_up_to_the_cap(c):
    _trading(c)
    r = c.run()
    assert len(r.opened) == MAX_OPEN_TRADES == 3
    assert r.rejected == {}  # the 4th and 5th symbols were never evaluated
    o = c.d.h.db.execute(
        "SELECT symbol, order_type, price, trigger_price, quantity, product FROM orders"
        " WHERE idempotency_key LIKE '%-entry' ORDER BY symbol"
    ).fetchall()
    assert [tuple(x) for x in o] == [
        (s, "sl_limit", 1008.05, 1006.0, 49, "mis") for s in sorted(SYMBOLS[:3])
    ]
    b = c.d.h.db.execute(
        "SELECT state, stop_price, target_price, trade_plan_id FROM intraday_brackets"
    ).fetchall()
    assert {(x[0], x[1], x[2]) for x in b} == {("entry_pending", 978.05, 1053.05)}
    plans = c.d.h.db.execute(
        "SELECT p.entry_strategy_id, s.feature_snapshot LIKE '%orb_high%' FROM trade_plans p"
        " JOIN signals s ON s.signal_id = p.signal_id WHERE p.plan_id = ?",
        (b[0][3],),
    ).fetchone()
    assert tuple(plans) == ("ID1", 1)  # live features kept with the signal (§3.4)


def test_full_session_blocks_another_cycle(c):
    _trading(c)
    c.run()
    c.d.at(10, 0)  # entries still pending (valid 30 min)
    assert c.run().skipped == "max_open_trades"


def test_open_and_cooldown_survive_a_new_cycle_object(c):
    """L2: derived from intraday_brackets, so a restart doesn't reset it."""
    _trading(c)
    c.watchlist = c.watchlist[:1]
    c.run()
    fresh = Cycle(c.d, watchlist=["TCS"])
    c.d.at(10, 0)
    assert fresh.run().rejected == {"TCS": "already_open"}
    bid = c.d.h.db.execute("SELECT bracket_id FROM intraday_brackets").fetchone()[0]
    c.d.at(10, 16)  # the 30-min entry validity has lapsed: cancelled, never filled
    assert c.d.h.bracket(bid)["state"] == "cancelled"
    assert fresh.run().rejected == {"TCS": "cooldown"}  # opened 09:45, < 60 min ago
    c.d.at(10, 46)
    assert len(fresh.run().opened) == 1


def test_breakout_fills_and_gets_protected(c):
    _trading(c)
    c.watchlist = c.watchlist[:1]
    c.run()
    c.d.at(9, 50, ltp=1000.0)  # session tick only (INFY), TCS still below
    c.d.h.tick(1007.0, sym="TCS")  # breaks the 1,006 trigger
    c.d.h.tick(1007.5, sym="TCS")
    c.d.at(9, 51)
    bid = c.d.h.db.execute("SELECT bracket_id FROM intraday_brackets").fetchone()[0]
    assert c.d.h.bracket(bid)["state"] == "open"  # filled, SL-M stop resting


def test_symbol_error_is_a_rejection_not_a_failure(c):
    _trading(c)
    c.batch_error = RuntimeError("feature store locked")
    r = c.run()
    assert set(r.rejected.values()) == {"error:RuntimeError"}
    assert c.cycle._failures == 0


def test_three_failed_cycles_halt_the_session(c):
    _trading(c)

    def boom(day):
        raise RuntimeError("watchlist table gone")

    c.cycle._watchlist = boom
    for _ in range(3):
        with pytest.raises(RuntimeError):
            c.run()
    assert c.d.state() == sm.HALTED
    assert c.d.row()["halt_reason"] == "cycle_failures"
    assert c.d.at(10, 0) == sm.HALTED  # not resumable today


def test_no_capital_ledger_skips(c):
    _trading(c)
    c.cycle._ledger = lambda: None
    assert c.run().skipped == "capital_unavailable"


def test_entry_refused_by_the_order_manager(c, monkeypatch):
    _trading(c)
    c.watchlist = c.watchlist[:1]

    def refuse(req):
        raise ValueError("trade rejected: stop_entry_target_not_ordered")

    monkeypatch.setattr(c.d.h.sup, "open_trade", refuse)
    assert c.run().rejected == {"TCS": "order_refused"}


def test_halted_mid_cycle_stops_opening(c, monkeypatch):
    _trading(c)
    real = c.d.h.sup.open_trade

    def open_then_pause(req):
        bid = real(req)
        c.d.bot_mode = "paused"
        c.d.ctl.tick()  # the minute tick halts the session between two entries
        return bid

    monkeypatch.setattr(c.d.h.sup, "open_trade", open_then_pause)
    r = c.run()
    assert len(r.opened) == 1
    assert c.d.state() == sm.HALTED
