"""A whole paper day through the production wiring (#116): Orchestrator + build_session +
build_decision_cycle, the registered tasks dispatched by the real Scheduler (rig and what
is seeded vs run: tests/orchestrator/production_day.py).

Worked example (the ORB case of tests/brain/test_intraday_decision.py, capital Rs 10 L):
  09:15-09:29 opening range 995 - 1,005, ATR 20
  09:30 cycle at LTP 1,000: stop-limit BUY trigger 1,006.00 / limit 1,008.05, 49 shares,
        stop 978.05, target 1,053.05
  09:31 LTP 1,007 -> trigger crossed, filled at 1,007.00 (<= limit), stop + target placed
  09:41 LTP 1,010 -> unrealised 49 x (1,010 - 1,007) = Rs 147
  10:30 LTP 1,040 >= entry + 1R (1,007 + 28.95): stop trails to 1,040 - 20 = 1,020.00
  11:00 on LTP 1,030 (stop 1,020 untouched) -> 15:14 square-off at market 1,030
  realised = 49 x (1,030 - 1,007) = Rs 1,127.00 (gross: the system books no charges)
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from executor import session_machine as sm
from executor.brokers.feeds.base import NIFTY_50, Tick
from executor.trading_mode import session_mode
from observability import telegram_feed as tg
from tests.orchestrator import production_day as pd

OPEN, CLOSE = 9 * 60 + 15, 15 * 60 + 30


def _ltp(minute: int) -> float:
    h, m = divmod(minute, 60)
    return {
        (9, 15): 995.0,  # opening range low
        (9, 16): 1005.0,  # opening range high
    }.get((h, m)) or (
        1000.0
        if minute < 9 * 60 + 31
        else 1007.0
        if minute < 9 * 60 + 40
        else 1010.0
        if minute < 10 * 60
        else 1020.0
        if minute < 10 * 60 + 30
        else 1040.0
        if minute == 10 * 60 + 30
        else 1030.0
    )


def _push(feed, now, ltp: float) -> None:
    feed.push(Tick(NIFTY_50, "NSE", 24100.0, now, prev_close=24000.0))  # +0.42%: confirms
    feed.push(
        Tick(
            "TCS",
            "NSE",
            ltp,
            now,
            day_open=999.0,
            prev_close=994.0,
            avg_traded_price=998.0,
            best_bid=ltp - 0.25,
            best_ask=ltp + 0.25,
        )
    )


class Day:
    """Runs the day minute by minute and records what the checks below need."""

    def __init__(self, tmp_path, monkeypatch, ticks_until: int = CLOSE) -> None:
        monkeypatch.delenv("BOOMER_TRADING_MODE", raising=False)  # unset: paper
        self.clock = pd.FakeClock(pd.RealDatetime(2026, 11, 19, 8, 45, tzinfo=pd.IST))
        self.clock.install(monkeypatch)
        self.alerts = pd.Alerts()
        monkeypatch.setattr("alerts.alerter.get_alerter", lambda: self.alerts)
        self.rig = pd.build(tmp_path, self.alerts)
        self.db = self.rig.db
        self.ticks_until = ticks_until
        self.log: list[tuple[str, str]] = []
        self.snapshots: dict[int, dict] = {}
        self.stop_triggers: list[float] = []
        self.breaches: list[str] = []
        self.out = MagicMock(return_value=True)  # send_telegram: no HTTP
        monkeypatch.setattr(tg, "send_telegram", self.out)
        monkeypatch.setattr(tg.time, "sleep", lambda s: None)
        self.poll()  # the feed starts before the day: cursor at 0, nothing to send yet

    def poll(self) -> int:
        return tg.poll_once(self.db, lambda text: tg.send_telegram("token", "chat", text))

    def q(self, sql: str, *args) -> list[tuple]:
        c = sqlite3.connect(self.db)
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def run(self) -> Day:
        self.probe = None
        for minute in range(8 * 60 + 45, CLOSE + 1):
            self.minute(minute)
        self.rig.emitter.stop()  # flush queued events
        # Every module that was imported during the day (lazily too) is on the fake clock.
        self.real_clock = self.clock.on_real_clock()
        self.fake_clock_modules = [
            m.__name__ for m in pd.src_modules() if m.__name__.startswith("executor.")
        ]
        self.sent = self.poll()  # the Telegram feed process catches up
        self.texts = [c.args[2] for c in self.out.call_args_list]
        self.repeat = self.poll()
        self.session_mode_if_armed = session_mode(self.db, pd.DAY, "live")
        self.broker_flat = not any(p.quantity for p in self.rig.session._broker.list_positions())
        self.active = self.rig.session.supervisor.active_count()
        return self

    def minute(self, minute: int) -> None:
        now = self.clock.at(*divmod(minute, 60))
        if minute <= self.ticks_until:
            _push(self.rig.feed, now, _ltp(minute))
        pd.dispatch_minute(self.rig.orc, now, self.log)
        self.snapshots[minute] = self.snapshot()
        if minute == 11 * 60:
            self.probe = self.rig.cycle.run()  # an extra decision cycle, off the schedule
        self.breaches += [f"{now:%H:%M} {b}" for b in self.unprotected()]

    def snapshot(self) -> dict:
        pos = self.q(
            "SELECT current_price, unrealised_pnl, realised_pnl, is_open FROM positions"
            " WHERE symbol='TCS'"
        )
        return {
            "state": self.rig.session.state(pd.DAY),
            "position": pos[0] if pos else None,
            "bracket": (self.q("SELECT state, trail_stop FROM intraday_brackets") or [None])[0],
        }

    def unprotected(self) -> list[str]:
        """Never unprotected: a long position is always covered by resting stops of at least
        its size (a trade mid-protect or mid-exit is the only excuse), and a stop never moves
        down."""
        broker = self.rig.session._broker
        if broker is None:  # execution is built lazily, on the first session tick
            return []
        sim = broker._kite
        out = []
        stops = [
            o
            for o in sim.orders()
            if o["order_type"] in ("SL", "SL-M")
            and o["transaction_type"] == "SELL"
            and o["status"] in ("TRIGGER PENDING", "OPEN", "MODIFY PENDING")
        ]
        transitional = {
            r[0]
            for r in self.q(
                "SELECT symbol FROM intraday_brackets WHERE state IN ('protecting','exiting')"
            )
        }
        for p in broker.list_positions():
            cover = sum(o["pending_quantity"] for o in stops if o["tradingsymbol"] == p.symbol)
            if p.quantity > 0 and cover < p.quantity and p.symbol not in transitional:
                out.append(f"{p.symbol} long {p.quantity}, stops cover {cover}")
        for o in stops:
            trigger = float(o["trigger_price"])
            if self.stop_triggers and trigger < self.stop_triggers[-1]:
                out.append(f"stop lowered {self.stop_triggers[-1]} -> {trigger}")
            self.stop_triggers.append(trigger)
        return out


def _play(tmp_path_factory, name: str, ticks_until: int) -> Day:
    """Play the day once (the fake clock and patches end with it); tests read the record."""
    with pytest.MonkeyPatch.context() as mp:
        return Day(tmp_path_factory.mktemp(name), mp, ticks_until).run()


@pytest.fixture(scope="module")
def paper_day(tmp_path_factory) -> Day:
    return _play(tmp_path_factory, "paper", CLOSE)


@pytest.fixture(scope="module")
def stale_day(tmp_path_factory) -> Day:
    return _play(tmp_path_factory, "stale", OPEN)


class TestWholePaperDay:
    @pytest.fixture(autouse=True)
    def _day(self, paper_day: Day):
        self.d = paper_day

    def test_no_module_is_left_on_the_real_clock(self):
        assert self.d.real_clock == []
        # the modules #123 split out are in play and covered
        for name in ("executor.order_store", "executor.session_context", "executor.order_checks"):
            assert name in self.d.fake_clock_modules

    def test_clock_is_fake_everywhere(self):
        row = self.d.q("SELECT created_at FROM trading_sessions")[0][0]
        assert row.startswith("2026-11-19T08:45")  # stamped by the fake 08:45, not wall time

    def test_tasks_ran_in_schedule_order(self):
        first = {}
        for hhmm, task in self.d.log:
            first.setdefault(task, hhmm)
        assert first["pre_market_executor_setup"] == "08:45"
        assert first["session_tick"] == "09:00"
        assert first["feed_health_check"] == "09:17"
        assert first["intraday_cycle"] == "09:00"  # a no-op until TRADING
        assert [t for _, t in self.d.log][0] == "pre_market_executor_setup"
        assert [h for h, _ in self.d.log] == sorted(h for h, _ in self.d.log)
        cycles = [h for h, t in self.d.log if t == "intraday_cycle"]
        assert cycles[:3] == ["09:00", "09:30", "10:00"] and cycles[-1] == "14:30"
        ticks = [h for h, t in self.d.log if t == "session_tick"]
        assert len(ticks) == 6 * 60 + 31  # every minute 09:00-15:30
        assert not self.d.q("SELECT task_id, status FROM task_runs WHERE status != 'SUCCESS'")
        assert not self.d.alerts.titles("critical")  # no task failed, feed always healthy

    def test_mode_resolved_paper_and_locked_on_the_session_row(self):
        assert self.d.q("SELECT mode FROM trading_sessions") == [("paper",)]
        # A server armed for live (and even an authorisation) cannot change today's row.
        assert self.d.session_mode_if_armed == "paper"
        assert str(self.d.rig.session._broker.broker_id) == "paper"
        assert {r[0] for r in self.d.q("SELECT DISTINCT mode FROM events WHERE mode != ''")} == {
            "paper"
        }

    def test_session_walked_its_states(self):
        s = self.d.snapshots
        assert s[9 * 60]["state"] == sm.CONNECTED or s[9 * 60]["state"] == sm.READY
        assert s[OPEN]["state"] == sm.TRADING
        assert s[14 * 60 + 30]["state"] == sm.NO_NEW_ENTRIES
        assert s[15 * 60 + 14]["state"] == sm.SQUARING_OFF
        assert s[CLOSE]["state"] == sm.CLOSED

    def test_cycle_entered_on_the_fixture_signal(self):
        entry = self.d.q(
            "SELECT order_type, side, product, quantity, price, trigger_price FROM orders"
            " WHERE idempotency_key LIKE '%-entry'"
        )
        assert entry == [("sl_limit", "buy", "mis", 49, 1008.05, 1006.0)]
        b = self.d.q("SELECT stop_price, target_price, trade_plan_id FROM intraday_brackets")[0]
        assert b[:2] == (978.05, 1053.05)
        assert self.d.q("SELECT stock_symbol FROM signals") == [("TCS",)]
        assert self.d.q("SELECT plan_id FROM trade_plans") == [(b[2],)]

    def test_every_order_is_mis_and_every_position_was_protected(self):
        assert self.d.q("SELECT DISTINCT product FROM orders") == [("mis",)]
        kinds = self.d.q("SELECT order_type, side, status FROM orders ORDER BY created_at")
        assert kinds == [
            ("sl_limit", "buy", "filled"),  # entry
            ("sl", "sell", "cancelled"),  # the resting stop (broker SL-M), cancelled at 15:14
            ("limit", "sell", "cancelled"),  # the target
            ("market", "sell", "filled"),  # square-off
        ]
        assert self.d.breaches == []
        assert self.d.stop_triggers[0] == 978.05 and self.d.stop_triggers[-1] == 1020.0
        assert self.d.stop_triggers == sorted(self.d.stop_triggers)  # only ever up

    def test_open_position_is_marked_to_market_mid_trade(self):  # #106
        s = self.d.snapshots
        assert s[9 * 60 + 32]["position"] == (1007.0, 0.0, 0.0, 1)
        assert s[9 * 60 + 41]["position"] == (1010.0, 147.0, 0.0, 1)  # 49 x (1,010 - 1,007)
        assert s[10 * 60 + 5]["position"] == (1020.0, 637.0, 0.0, 1)  # 49 x 13
        assert s[12 * 60]["position"] == (1030.0, 1127.0, 0.0, 1)  # 49 x 23

    def test_stop_trailed_up_at_one_r(self):
        s = self.d.snapshots
        assert s[10 * 60 + 29]["bracket"] == ("open", None)  # high 1,020 < 1R (1,035.95)
        assert s[10 * 60 + 31]["bracket"] == ("open", 1020.0)  # 1,040 - ATR 20
        moves = self.d.q("SELECT payload FROM events WHERE name='stop_raised'")
        inputs = json.loads(moves[0][0])["inputs"]
        assert len(moves) == 1 and (inputs["from"], inputs["to"]) == (978.05, 1020.0)

    def test_trade_closed_with_the_hand_computed_pnl(self):
        row = self.d.q(
            "SELECT state, filled_quantity, exit_reason, realised_pnl FROM intraday_brackets"
        )
        assert row == [("closed", 49, "squareoff", 1127.0)]  # 49 x (1,030 - 1,007)
        assert self.d.q("SELECT is_open, realised_pnl, current_price FROM positions") == [
            (0, 1127.0, 1030.0)
        ]
        exit_fill = self.d.q("SELECT average_fill_price FROM orders WHERE order_type='market'")
        assert exit_fill == [(1030.0,)]
        assert self.d.broker_flat and self.d.active == 0

    def test_events_carry_the_trade_story(self):
        names = [
            r[0]
            for r in self.d.q(
                "SELECT name FROM events WHERE entity_type='trade' AND kind='state_transition'"
                " ORDER BY event_id"
            )
        ]
        assert names == [
            "created",
            "entry_filled",
            "stop_confirmed",
            "stop_raised",
            "square_off",
            "corrective_order",
            "flat",
        ]
        session = [
            r[0]
            for r in self.d.q(
                "SELECT name FROM events WHERE entity_type='session' AND kind='state_transition'"
                " ORDER BY event_id"
            )
        ]
        assert session == [
            "created",
            "setup_ok",
            "watchlist_ready",
            "market_open",
            "entry_cutoff",
            "square_off",
            "all_flat",
        ]

    def test_telegram_feed_sends_one_message_per_trade_event(self):
        rows = self._events()
        renderable = [r for r in rows if r["entity_type"] == "trade" and tg.render(r)]
        texts = self.d.texts
        assert len(texts) == self.d.sent
        trade_msgs = [t for t in texts[:-1] if "TCS" in t]  # not the EOD summary
        assert len(trade_msgs) == len(renderable) == 6  # corrective_order is order noise
        assert [m.split("</b> ")[1] for m in trade_msgs] == [
            "entry placed: 49 @ 1,008.05 (stop 978.05, target 1,053.05)",
            "entry filled: 49 @ 1007.0",
            "protected: stop 978.05, target 1053.05",
            "stop raised to 1020.0",
            "squaring off",
            "closed: +₹1,127",
        ]
        assert self.d.out.call_args_list[0].args[:2] == ("token", "chat")
        assert self.d.repeat == 0  # nothing sent twice

    def test_end_of_day_summary_reads_the_right_totals(self):
        texts = self.d.texts
        assert "Session closed" in texts[-2] and texts[-1].startswith("<b>")
        summary = texts[-1].splitlines()
        assert summary == [
            "<b>\U0001f3c1 End of day — 2026-11-19 (PAPER)</b>",
            "Trades: 1 (1 closed: 1 won, 0 lost)",
            "Realised P&amp;L: <b>+₹1,127</b>",
            "  TCS: squareoff, +₹1,127",
            "Exits forced by a missing stop: 0",
        ]

    def _events(self) -> list[sqlite3.Row]:
        c = sqlite3.connect(self.d.db)
        c.row_factory = sqlite3.Row
        try:
            return c.execute(
                "SELECT * FROM events WHERE kind='state_transition' ORDER BY event_id"
            ).fetchall()
        finally:
            c.close()


class TestStaleFeedHaltsTheDay:
    """Ticks stop after 09:15 and never return: fail closed, alert, no order all day."""

    @pytest.fixture(autouse=True)
    def _day(self, stale_day: Day):
        self.d = stale_day

    def test_session_halts_on_feed_stale_and_stays_out(self):
        s = self.d.snapshots
        assert s[OPEN]["state"] == sm.TRADING  # fresh at the open
        assert s[9 * 60 + 17]["state"] == sm.HALTED
        for minute in (9 * 60 + 30, 11 * 60, 14 * 60 + 29):
            assert s[minute]["state"] == sm.HALTED  # never recovers without ticks
        assert self.d.q("SELECT halt_reason FROM trading_sessions") == [("feed_stale",)]
        assert s[CLOSE]["state"] == sm.CLOSED  # squared off (nothing to) and closed at 15:14
        halts = self.d.q("SELECT payload FROM events WHERE entity_type='session' AND name='halt'")
        assert [json.loads(h[0])["rule"] for h in halts] == ["feed_stale"]

    def test_no_order_and_no_trade_all_day(self):
        assert self.d.q("SELECT COUNT(*) FROM orders") == [(0,)]
        assert self.d.q("SELECT COUNT(*) FROM intraday_brackets") == [(0,)]
        assert self.d.q("SELECT COUNT(*) FROM positions") == [(0,)]
        assert not self.d.rig.session._broker.list_positions()

    def test_the_decision_cycle_declines_while_halted(self):
        result = self.d.probe  # a cycle run at 11:00, off the schedule
        assert result.skipped == "session_halted" and result.opened == []
        cycles = [h for h, t in self.d.log if t == "intraday_cycle"]
        assert "09:30" in cycles and "14:30" in cycles  # it ran, and every run was a no-op
        assert self.d.q("SELECT COUNT(*) FROM signals") == [(0,)]  # nothing was even scored

    def test_alerts_were_sent(self):
        critical = self.d.alerts.titles("critical")
        assert "Market data feed not ticking after open" in critical  # 09:17 health check
        assert any("Trading HALTED" in t for t in self.d.alerts.titles("warn") + critical)
        body = [
            b for lv, t, b in self.d.alerts.sent if t == "Market data feed not ticking after open"
        ]
        assert "last_tick_age_s=120" in body[0]  # last tick 09:15, checked 09:17

    def test_telegram_feed_announces_the_halt_and_a_flat_close(self):
        texts = self.d.texts
        assert any("Session HALTED</b>: feed_stale" in t for t in texts)
        assert "Trades: 0 (0 closed: 0 won, 0 lost)" in texts[-1]
        assert self.d.snapshots[CLOSE]["state"] == sm.CLOSED
