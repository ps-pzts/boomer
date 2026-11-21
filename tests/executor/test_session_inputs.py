"""Live breaker inputs and the reconcile report (#63).

Worked example (defaults: intraday daily loss 2%, 3 consecutive losses,
NIFTY pause 3%), capital ₹10,00,000, all of it intraday:
  realised -₹19,999 -> 1.9999% -> clear
  realised -₹20,000 -> 2.0000% -> intraday_daily_loss (and portfolio_daily_loss) trip
  NIFTY prev close 24,000, ltp 23,280 -> -3.0% -> nifty_move trips
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from capital.models import CapitalLedgerRow
from capital.risk_config import RiskConfigStore
from db.migrations import run_migrations
from executor.session_inputs import (
    LegStatus,
    PnlToday,
    net_by_symbol,
    nifty_move_pct,
    realised_pnl,
    reconcile_report,
    tripped_breakers,
)

IST = ZoneInfo("Asia/Kolkata")
_MIGRATIONS = Path(__file__).parents[2] / "migrations"
DAY = date(2026, 11, 19)  # a Thursday
NOW = datetime(2026, 11, 19, 11, 0, tzinfo=IST)


def _ledger(capital: str = "1000000", hwm: str | None = None) -> CapitalLedgerRow:
    c = Decimal(capital)
    return CapitalLedgerRow(
        ledger_id="l1",
        as_of_date=DAY,
        total_capital=c,
        total_cash=c,
        intraday_allocated_pct=Decimal("1.00"),
        intraday_deployed=Decimal("0"),
        high_water_mark=Decimal(hwm) if hwm else c,
        eod_drawdown_pct=Decimal("0"),
        consecutive_loss_days=0,
        peak_date=DAY,
    )


@pytest.fixture()
def config(tmp_path):
    db = str(tmp_path / "c.db")
    run_migrations(db, _MIGRATIONS)
    return RiskConfigStore(db).seed_defaults(DAY)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db = str(tmp_path / "b.db")
    run_migrations(db, _MIGRATIONS)
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys=OFF")
    return c


def _closed(conn, day: str, pnl: float, closed_at: str, n: list[int] = [0]) -> None:  # noqa: B006
    n[0] += 1
    conn.execute(
        "INSERT INTO intraday_brackets (bracket_id, session_date, symbol, exchange,"
        " planned_quantity, entry_price, stop_price, target_price, atr, entry_order_id,"
        " entry_expires_at, state, realised_pnl, created_at, updated_at, closed_at)"
        " VALUES (?, ?, 'INFY', 'NSE', 1, 100, 98, 104, 2, 'o', 'x', 'closed', ?, 'c', 'u', ?)",
        (f"b{n[0]}", day, pnl, closed_at),
    )
    conn.commit()


class TestRealisedPnl:
    def test_sum_streak_and_week(self, conn):
        _closed(conn, "2026-11-16", -500.0, "2026-11-16T10:00")  # Monday: week only
        _closed(conn, "2026-11-19", 300.0, "2026-11-19T09:40")
        _closed(conn, "2026-11-19", -100.0, "2026-11-19T10:10")
        _closed(conn, "2026-11-19", -200.0, "2026-11-19T10:40")
        _closed(conn, "2026-11-13", -9999.0, "2026-11-13T10:00")  # last week: ignored
        p = realised_pnl(conn, DAY)
        assert p.realised == Decimal("0")  # 300 - 100 - 200
        assert p.consecutive_losses == 2  # the win at 09:40 breaks the streak
        assert p.realised_week == Decimal("-500")

    def test_flat_trade_ends_the_losing_streak(self, conn):
        _closed(conn, "2026-11-19", -100.0, "2026-11-19T09:40")
        _closed(conn, "2026-11-19", 0.0, "2026-11-19T10:10")  # breakeven is not a loss
        assert realised_pnl(conn, DAY).consecutive_losses == 0
        _closed(conn, "2026-11-19", -50.0, "2026-11-19T10:40")
        assert realised_pnl(conn, DAY).consecutive_losses == 1  # only the one after it

    def test_small_win_ends_the_losing_streak(self, conn):
        _closed(conn, "2026-11-19", -100.0, "2026-11-19T09:40")
        _closed(conn, "2026-11-19", 0.5, "2026-11-19T10:10")
        assert realised_pnl(conn, DAY).consecutive_losses == 0

    def test_null_pnl_counts_as_zero(self, conn):
        _closed(conn, "2026-11-19", 10.0, "2026-11-19T09:40")
        conn.execute(
            "UPDATE intraday_brackets SET realised_pnl=NULL WHERE closed_at='2026-11-19T09:40'"
        )
        _closed(conn, "2026-11-19", -3.0, "2026-11-19T10:10")
        got = realised_pnl(conn, DAY)
        assert (got.realised, got.consecutive_losses) == (Decimal("-3.0"), 1)

    def test_no_trades(self, conn):
        assert realised_pnl(conn, DAY) == PnlToday(Decimal("0"), 0, Decimal("0"))


class TestNiftyMove:
    def test_worked_example(self):
        assert nifty_move_pct(24000.0, 23280.0) == Decimal("-0.03")

    @pytest.mark.parametrize("prev, ltp", [(None, 100.0), (0.0, 100.0), (100.0, None)])
    def test_unknown_is_zero(self, prev, ltp):
        assert nifty_move_pct(prev, ltp) == Decimal("0")


class TestTrippedBreakers:
    def _run(self, config, realised="0", streak=0, week="0", nifty="0", hwm=None):
        return tripped_breakers(
            pnl=PnlToday(Decimal(realised), streak, Decimal(week)),
            ledger=_ledger(hwm=hwm),
            nifty_move=Decimal(nifty),
            now=NOW,
            config=config,
        )

    def test_all_clear(self, config):
        assert self._run(config) == []

    def test_daily_loss_boundary(self, config):
        assert self._run(config, realised="-19999") == []
        assert self._run(config, realised="-20000") == [
            "breaker:intraday_daily_loss",
            "breaker:portfolio_daily_loss",
        ]

    def test_consecutive_losses(self, config):
        assert self._run(config, streak=2) == []
        assert self._run(config, streak=3) == ["breaker:consecutive_losses"]

    def test_nifty_move_is_listed_last(self, config):
        """Resumable reasons go last, so a halt records the non-resumable cause."""
        assert self._run(config, streak=3, nifty="-0.03") == [
            "breaker:consecutive_losses",
            "breaker:nifty_move",
        ]

    def test_weekly_loss(self, config):
        assert self._run(config, week="-40000") == ["breaker:portfolio_weekly_loss"]

    def test_drawdown_from_high_water_mark(self, config):
        # hwm 10.9L, capital 10L: (10.9 - 10) / 10.9 = 8.26% >= 8%
        assert self._run(config, hwm="1090000") == ["breaker:portfolio_max_drawdown"]

    def test_profit_raises_live_capital_for_drawdown(self, config):
        # capital 10L + realised +50,000 = live 10.5L; hwm 11L -> 4.5% drawdown: clear.
        # (live = capital - realised = 9.5L would read 13.6% and halt a winning day)
        got = tripped_breakers(
            pnl=PnlToday(Decimal("50000"), 0, Decimal("50000")),
            ledger=_ledger(hwm="1100000"),
            nifty_move=Decimal("0"),
            now=NOW,
            config=config,
        )
        assert got == []

    def test_loss_below_hwm_trips_drawdown(self, config):
        # capital 1,000 (all intraday), realised -500 -> live 500, hwm 1,000 -> 50%
        got = tripped_breakers(
            pnl=PnlToday(Decimal("-500"), 0, Decimal("-500")),
            ledger=_ledger(capital="1000"),
            nifty_move=Decimal("0"),
            now=NOW,
            config=config,
        )
        assert "breaker:portfolio_max_drawdown" in got
        # and a 1-rupee capital still computes the drawdown (hwm 1, live 0.5 -> 50%)
        got = tripped_breakers(
            pnl=PnlToday(Decimal("-0.5"), 0, Decimal("-0.5")),
            ledger=_ledger(capital="1"),
            nifty_move=Decimal("0"),
            now=NOW,
            config=config,
        )
        assert "breaker:portfolio_max_drawdown" in got

    def test_zero_capital_is_clear_not_a_crash(self, config):
        got = tripped_breakers(
            pnl=PnlToday(Decimal("0"), 0, Decimal("0")),
            ledger=_ledger(capital="0", hwm="0"),
            nifty_move=Decimal("0"),
            now=NOW,
            config=config,
        )
        assert got == []

    def test_late_entry_is_not_a_halt(self, config):
        late = tripped_breakers(
            pnl=PnlToday(Decimal("0"), 0, Decimal("0")),
            ledger=_ledger(),
            nifty_move=Decimal("0"),
            now=datetime(2026, 11, 19, 14, 45, tzinfo=IST),
            config=config,
        )
        assert late == []  # the entry_cutoff transition owns 14:30


def _leg(leg="stop", resting=True, sym="INFY") -> LegStatus:
    return LegStatus("b1", sym, leg, "o1", resting)


class TestReconcileReport:
    def _report(self, **kw):
        args = dict(
            trade_net={"INFY": 10},
            broker_net={"INFY": 10},
            open_legs=[_leg("stop"), _leg("target")],
            feed_fresh=True,
            watchlist_size=50,
        )
        args.update(kw)
        return reconcile_report(**args)

    def test_clean(self):
        r = self._report()
        assert r.clean and r.failures() == []

    def test_stop_filled_during_outage(self):
        r = self._report(broker_net={})
        assert r.failures() == ["net_quantity_matches_broker"]
        assert r.checks["net_quantity_matches_broker"]["mismatch"] == {
            "INFY": {"ours": 10, "broker": 0}
        }

    def test_manual_trade_in_kite_app(self):
        r = self._report(broker_net={"INFY": 10, "TCS": 5})
        assert set(r.failures()) == {"net_quantity_matches_broker", "no_untracked_broker_position"}
        assert r.checks["no_untracked_broker_position"]["symbols"] == ["TCS"]

    def test_stop_gone(self):
        r = self._report(open_legs=[_leg("stop", resting=False), _leg("target")])
        assert set(r.failures()) == {"tracked_legs_resting", "open_trades_have_stop"}

    def test_target_gone_is_not_an_unprotected_stop(self):
        r = self._report(open_legs=[_leg("stop"), _leg("target", resting=False)])
        assert r.failures() == ["tracked_legs_resting"]

    def test_feed_and_watchlist(self):
        r = self._report(feed_fresh=False, watchlist_size=0)
        assert set(r.failures()) == {"feed_fresh", "watchlist_present"}

    def test_as_inputs_is_json_ready(self):
        import json

        assert json.loads(json.dumps(self._report().as_inputs()))["clean"] is True


def test_pnl_and_leg_records_are_immutable():
    with pytest.raises(AttributeError):
        PnlToday(Decimal("0"), 0, Decimal("0")).realised = Decimal("1")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        LegStatus("b", "INFY", "stop", "o1", True).resting = False  # type: ignore[misc]


class TestReconcileMismatchDetail:
    def _mismatch(self, trade_net, broker_net):
        r = reconcile_report(
            trade_net=trade_net, broker_net=broker_net, open_legs=[], feed_fresh=True,
            watchlist_size=1,
        )  # fmt: skip
        return r.checks["net_quantity_matches_broker"]["mismatch"]

    def test_position_only_at_the_broker(self):
        assert self._mismatch({}, {"A": 1}) == {"A": {"ours": 0, "broker": 1}}
        assert self._mismatch({}, {"A": 5}) == {"A": {"ours": 0, "broker": 5}}

    def test_position_only_in_our_books(self):
        assert self._mismatch({"A": 1}, {}) == {"A": {"ours": 1, "broker": 0}}

    def test_matching_and_differing(self):
        got = self._mismatch({"A": 10, "B": 5}, {"A": 10, "B": 7})
        assert got == {"B": {"ours": 5, "broker": 7}}

    def test_one_stock_watchlist_is_present(self):
        r = reconcile_report(
            trade_net={}, broker_net={}, open_legs=[], feed_fresh=True, watchlist_size=1
        )
        assert r.checks["watchlist_present"] == {"ok": True, "size": 1}


def test_net_by_symbol_mis_only_and_drops_zero():
    pos = [
        SimpleNamespace(symbol="INFY", quantity=10, product="mis"),
        SimpleNamespace(symbol="INFY", quantity=-10, product="mis"),
        SimpleNamespace(symbol="TCS", quantity=5, product="mis"),
        SimpleNamespace(symbol="HDFC", quantity=7, product="cnc"),
    ]
    assert net_by_symbol(pos) == {"TCS": 5}
