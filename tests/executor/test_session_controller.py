"""SessionController end to end on the paper broker (#63, designs/state-machines.md §4).

Real OrderManager / PositionManager / TradeSupervisor / PaperBroker / ManualFeed
(the Trade machine harness), with the clock moved through one trading day.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from capital.risk_config import RiskConfigStore
from executor import session_machine as sm
from executor.brokers.feeds.base import NIFTY_50, Tick
from executor.models import OrderRequest, OrderSide, OrderType, ProductType
from executor.session_controller import SessionController
from tests.executor.test_session_inputs import DAY, _ledger
from tests.executor.test_trade_lifecycle import Harness, _req

IST = ZoneInfo("Asia/Kolkata")


class Day:
    def __init__(self, tmp_path, supervisor: bool = True) -> None:
        self.h = Harness(tmp_path)
        self.h.feed.connect()
        self.bot_mode = "auto"
        self.watchlist = 50
        self.ledger = _ledger()
        self.nifty_prev, self.nifty = 24000.0, 24000.0
        db_path = self.h.db.execute("PRAGMA database_list").fetchone()[2]
        config = RiskConfigStore(db_path).seed_defaults(DAY)
        self.ctl = SessionController(
            self.h.db,
            mode="paper",
            feed=self.h.feed,
            supervisor=self.h.sup if supervisor else None,
            broker=self.h.paper,
            order_manager=self.h.om,
            bot_mode=lambda: self.bot_mode,
            watchlist_size=lambda d: self.watchlist,
            ledger=lambda: self.ledger,
            risk_config=lambda: config,
            alerter=self.h.alerter,
            clock=self.h.clock,
        )

    def at(self, hh: int, mm: int, *, fresh: bool = True, ltp: float = 100.0) -> str:
        """Move the clock, push fresh ticks (unless the feed is stale), tick once."""
        self.h.clock.now = datetime(2026, 11, 19, hh, mm, tzinfo=IST)
        if fresh:
            self.h.feed.push(
                Tick(
                    symbol=NIFTY_50,
                    exchange="NSE",
                    ltp=self.nifty,
                    prev_close=self.nifty_prev,
                    received_at=self.h.clock.now,
                )
            )
            self.h.tick(ltp)
        return self.ctl.tick()

    def trading(self) -> None:
        for hh, mm in ((9, 0), (9, 1), (9, 15)):
            self.at(hh, mm)
        assert self.state() == sm.TRADING

    def state(self) -> str:
        return self.ctl.state(DAY)

    def row(self):
        return self.h.db.execute(
            "SELECT * FROM trading_sessions WHERE session_date=?", (DAY.isoformat(),)
        ).fetchone()

    def history(self) -> list[tuple[str, str]]:
        rows = self.h.db.execute(
            "SELECT kind, name FROM events WHERE entity_type='session' ORDER BY event_id"
        ).fetchall()
        return [(k, n) for k, n in rows]

    def transitions(self) -> list[str]:
        return [n for k, n in self.history() if k == "state_transition"]

    def broker_trade(self, side: OrderSide, sym: str = "TCS", qty: int = 5) -> None:
        """A trade placed outside the bot (e.g. in the Kite app)."""
        self.h.paper.place_order(
            OrderRequest(
                symbol=sym,
                exchange="NSE",
                side=side,
                order_type=OrderType.MARKET,
                quantity=qty,
                product=ProductType.MIS,
                idempotency_key=f"manual-{side}",
            )
        )
        self.h.tick(500.0, sym=sym)


@pytest.fixture()
def d(tmp_path) -> Day:
    return Day(tmp_path)


class TestNormalDay:
    def test_setup_to_trading(self, d):
        assert d.at(9, 0) == sm.CONNECTED  # created + feed connected in one tick
        assert not d.ctl.entries_allowed(DAY)
        assert d.at(9, 1) == sm.READY
        assert d.at(9, 14) == sm.READY  # not before the open: no attempt, no event
        assert d.at(9, 15) == sm.TRADING
        assert d.ctl.entries_allowed(DAY)
        assert d.transitions() == ["created", "setup_ok", "watchlist_ready", "market_open"]

    def test_ensure_is_idempotent(self, d):
        assert d.ctl.ensure(DAY) == sm.PRE_OPEN
        assert d.ctl.ensure(DAY) == sm.PRE_OPEN
        assert d.transitions() == ["created"]
        assert d.row()["mode"] == "paper"

    def test_square_off_closes_trade_then_session(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req())
        d.at(10, 0)  # entry fills, stop + target placed
        assert d.h.bracket(bid)["state"] == "open"
        d.at(14, 30)
        assert d.state() == sm.NO_NEW_ENTRIES
        assert d.at(15, 14, ltp=101.0) == sm.SQUARING_OFF  # market exit placed
        assert d.at(15, 15, ltp=101.0) == sm.CLOSED  # exit filled, broker flat
        b = d.h.bracket(bid)
        assert (b["state"], b["exit_reason"]) == ("closed", "squareoff")
        assert d.h.paper.list_positions() == []
        assert d.transitions()[-3:] == ["entry_cutoff", "square_off", "all_flat"]
        assert d.at(15, 16) == sm.CLOSED  # terminal: later ticks are no-ops

    def test_never_traded_is_abandoned(self, d):
        d.h.feed.close()
        d.at(9, 0)
        assert d.state() == sm.PRE_OPEN
        assert d.at(15, 14) == sm.ABANDONED

    def test_open_trades_managed_every_tick(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req())
        d.at(10, 0)
        d.at(10, 1, ltp=104.2)  # target hit
        d.at(10, 2, ltp=104.2)
        assert d.h.bracket(bid)["state"] == "closed"


class TestHalts:
    def test_feed_stale_cycle(self, d):
        d.trading()
        d.at(9, 16)
        assert d.at(9, 18, fresh=False) == sm.HALTED
        assert d.row()["halt_reason"] == "feed_stale"
        assert not d.ctl.entries_allowed(DAY)
        assert d.at(9, 19, fresh=False) == sm.HALTED  # cause still active
        assert d.at(9, 20) == sm.TRADING  # recover -> reconcile (clean) -> trading
        assert d.row()["halt_reason"] is None
        assert d.transitions()[-3:] == ["halt", "recover", "reconciled"]

    def test_loss_breaker_holds_for_the_day(self, d):
        d.trading()
        d.ledger = _ledger(capital="1000")  # 2% of ₹1,000 = ₹20
        bid = d.h.sup.open_trade(_req())
        d.at(10, 0)
        d.at(10, 1, ltp=97.0)  # stop 98 gapped: (97 - 100) x 10 = -30 <= -20
        d.at(10, 2, ltp=97.0)
        assert d.h.bracket(bid)["state"] == "closed"
        assert d.at(10, 3) == sm.HALTED
        assert d.row()["halt_reason"] == "breaker:intraday_daily_loss"
        assert d.at(11, 0) == sm.HALTED  # not resumable: no recover attempt at all
        assert "recover" not in [n for _, n in d.history()]
        assert "Trading HALTED — breaker:intraday_daily_loss" in d.h.critical_titles()

    def test_nifty_move_halts_and_resumes(self, d):
        d.trading()
        d.nifty = 23280.0  # -3%
        assert d.at(10, 0) == sm.HALTED
        assert d.row()["halt_reason"] == "breaker:nifty_move"
        d.nifty = 23500.0  # back inside
        assert d.at(10, 1) == sm.TRADING

    def test_paused_halts_and_resumes(self, d):
        d.trading()
        d.bot_mode = "paused"
        assert d.at(10, 0) == sm.HALTED
        d.bot_mode = "auto"
        assert d.at(10, 1) == sm.TRADING

    def test_no_resume_after_cutoff(self, d):
        d.trading()
        d.at(14, 0, fresh=False)
        assert d.state() == sm.HALTED
        assert d.at(14, 30) == sm.NO_NEW_ENTRIES

    def test_unreadable_breakers_fail_closed(self, d):
        d.trading()

        def boom():
            raise RuntimeError("db locked")

        d.ctl._ledger = boom
        assert d.at(10, 0) == sm.HALTED
        assert d.row()["halt_reason"] == "breakers_unavailable:RuntimeError"

    def test_missing_ledger_blocks_market_open(self, d):
        d.ledger = None
        d.at(9, 0)
        d.at(9, 1)
        assert d.at(9, 15) == sm.READY
        blocked = d.h.db.execute(
            "SELECT payload FROM events WHERE kind='transition_blocked' AND name='market_open'"
        ).fetchone()[0]
        assert json.loads(blocked)["blocked_by"] == "capital_unavailable"


class TestReconcile:
    def test_untracked_broker_position_halts_until_fixed_and_acked(self, d):
        d.trading()
        d.at(9, 16)
        d.at(9, 18, fresh=False)  # halted: feed_stale
        d.broker_trade(OrderSide.BUY)  # manual buy in the Kite app during the outage
        assert d.at(9, 20) == sm.HALTED  # recover -> reconcile_failed
        assert d.row()["halt_reason"] == "reconcile_mismatch"
        assert "Reconcile MISMATCH — trading halted" in d.h.critical_titles()
        assert d.at(9, 21) == sm.HALTED  # waits for the operator
        d.ctl.acknowledge_mismatch(DAY, actor="operator")
        assert d.at(9, 22) == sm.HALTED  # acked, but still wrong at the broker
        assert d.row()["halt_acked_at"] is None  # a fresh failure needs a fresh ack
        d.broker_trade(OrderSide.SELL)  # operator flattens it
        d.ctl.acknowledge_mismatch(DAY, actor="operator")
        assert d.at(9, 23) == sm.TRADING

    def test_ack_requires_actor(self, d):
        with pytest.raises(ValueError):
            d.ctl.acknowledge_mismatch(DAY, actor="")

    def test_restart_reconciles_before_trading(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req())
        d.at(10, 0)
        d.ctl.on_restart(DAY)
        assert d.state() == sm.RECONCILING
        assert not d.ctl.entries_allowed(DAY)
        assert d.at(10, 1) == sm.TRADING  # stop resting, net 10 == broker 10
        assert d.h.bracket(bid)["state"] == "open"

    def test_restart_before_open_waits_for_the_open(self, d):
        d.at(9, 0)
        d.at(9, 1)
        d.ctl.on_restart(DAY)
        assert d.at(9, 5) == sm.RECONCILING  # clean, but the market isn't open
        assert d.at(9, 15) == sm.TRADING

    def test_restart_in_halted_stays_halted(self, d):
        d.trading()
        d.at(9, 16)
        d.at(9, 18, fresh=False)
        d.ctl.on_restart(DAY)
        assert d.state() == sm.HALTED


def test_without_execution_never_trades(tmp_path):
    d = Day(tmp_path, supervisor=False)
    d.at(9, 0)
    d.at(9, 1)
    assert d.at(9, 15) == sm.READY
    assert d.at(15, 14) == sm.ABANDONED


def test_halt_reason_and_event_commit_together(d):
    d.trading()
    d.bot_mode = "paused"
    d.at(10, 0)
    payload = d.h.db.execute(
        "SELECT payload FROM events WHERE kind='state_transition' AND name='halt'"
    ).fetchone()[0]
    p = json.loads(payload)
    assert (p["rule"], p["to_state"]) == ("paused", "halted")
    assert p["inputs"]["bot_mode"] == "paused"
    assert d.row()["halt_reason"] == "paused"
    assert d.row()["halt_acked_at"] is None


def test_every_reconcile_emits_a_decision_event(d, monkeypatch):
    """§4.5: observability sees why trading did or didn't resume."""
    import observability.emit as obs

    seen = []
    monkeypatch.setattr(obs, "emit", lambda kind, name, **kw: seen.append((kind, name, kw)))
    d.trading()
    d.ctl.on_restart(DAY)
    d.at(10, 0)
    ((kind, name, kw),) = [s for s in seen if s[1] == "reconcile_report"]
    assert (kind, kw["clean"], kw["entity_id"]) == ("decision", True, DAY.isoformat())
    assert set(kw["checks"]) >= {"net_quantity_matches_broker", "open_trades_have_stop"}


class TestEmergencyStop:
    """Operator rule (#67): emergency stop flattens everything at once, ends the day."""

    def test_flattens_open_trade_immediately_and_closes(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req())
        d.at(10, 0)
        assert d.h.bracket(bid)["state"] == "open"
        d.bot_mode = "emergency_stop"
        assert d.at(10, 1, ltp=101.0) == sm.SQUARING_OFF  # not 15:14: now
        assert d.at(10, 2, ltp=101.0) == sm.CLOSED
        b = d.h.bracket(bid)
        assert (b["state"], b["exit_reason"]) == ("closed", "squareoff")
        assert d.h.paper.list_positions() == []
        assert "EMERGENCY STOP — flattening everything" in d.h.critical_titles()
        square = d.h.db.execute(
            "SELECT payload FROM events WHERE entity_type='session' AND name='square_off'"
        ).fetchone()[0]
        assert json.loads(square)["rule"] == "emergency_stop"

    def test_cancels_pending_entry(self, d):
        d.trading()
        bid = d.h.sup.open_trade(_req(entry=99.0))  # LIMIT 99 below the 100 market
        d.bot_mode = "emergency_stop"
        assert d.at(10, 0) == sm.CLOSED  # nothing filled: cancelled, already flat
        assert d.h.bracket(bid)["state"] == "cancelled"

    def test_flattens_position_the_bot_did_not_open(self, d):
        d.trading()
        d.broker_trade(OrderSide.BUY)  # manual buy in the Kite app
        d.bot_mode = "emergency_stop"
        assert d.at(10, 0) == sm.CLOSED  # paper fills the market exit at once
        keys = [
            r[0]
            for r in d.h.db.execute(
                "SELECT idempotency_key FROM orders WHERE idempotency_key LIKE 'emergency-%'"
            )
        ]
        assert keys == ["emergency-2026-11-19-TCS-1"]
        assert d.h.paper.list_positions() == []

    def test_short_position_is_bought_back(self, d):
        d.trading()
        d.broker_trade(OrderSide.SELL)
        d.bot_mode = "emergency_stop"
        d.at(10, 0)
        side = d.h.db.execute(
            "SELECT side FROM orders WHERE idempotency_key='emergency-2026-11-19-TCS-1'"
        ).fetchone()[0]
        assert side == "buy"

    def test_before_trading_with_nothing_open_abandons(self, d):
        d.at(9, 0)
        d.bot_mode = "emergency_stop"
        assert d.at(9, 1) == sm.ABANDONED

    def test_no_resume_afterwards(self, d):
        d.trading()
        d.bot_mode = "emergency_stop"
        assert d.at(10, 0) == sm.CLOSED
        d.bot_mode = "auto"
        assert d.at(10, 1) == sm.CLOSED
        assert not d.ctl.entries_allowed(DAY)


def test_lazy_execution_unblocks_the_open(tmp_path):
    """#74: execution unavailable at boot (no ledger yet) is retried every tick."""
    d = Day(tmp_path, supervisor=False)
    ready = {"ok": False}
    d.ctl._broker = None
    d.ctl._execution_factory = lambda: (d.h.sup, d.h.paper, d.h.om) if ready["ok"] else None
    d.at(9, 0)
    d.at(9, 1)
    assert d.at(9, 15) == sm.READY  # blocked: execution_unavailable
    ready["ok"] = True  # e.g. the 08:45 setup bootstrapped the capital ledger late
    assert d.at(9, 16) == sm.TRADING
    assert d.ctl.supervisor is d.h.sup
