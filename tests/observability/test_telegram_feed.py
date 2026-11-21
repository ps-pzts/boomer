"""#103: the live Telegram feed and the end-of-day summary, driven by a real day on the
hostile paper broker (so the events are the ones the system really writes).

Worked numbers (qty 10, entry 100, stop 98, target 104): target hit -> +₹40."""

from __future__ import annotations

import pytest

from observability import telegram_feed as feed
from tests.alerts.plain import is_card, plain
from tests.executor.test_trade_lifecycle import _req
from tests.integration.harness import Hostile
from tests.integration.test_scenarios import open_protected


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(feed.time, "sleep", lambda s: None)


@pytest.fixture()
def h(tmp_path) -> Hostile:
    return Hostile(tmp_path)


def db_path(h: Hostile) -> str:
    return h.db.execute("PRAGMA database_list").fetchone()[2]


class Outbox:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[str] = []
        self.ok = ok

    def __call__(self, text: str) -> bool:
        if self.ok:
            self.sent.append(text)
        return self.ok


def start_feed(h: Hostile) -> Outbox:
    """First poll on an empty log: sets the cursor to 'now' and sends nothing."""
    out = Outbox()
    assert feed.poll_once(db_path(h), out) == 0
    return out


class TestTradeMessages:
    def test_a_winning_trade_tells_its_story(self, h):
        out = start_feed(h)
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.sup.reconcile()
        h.tick(104.2)
        h.step()
        h.step()
        feed.poll_once(db_path(h), out)
        assert out.sent and all(is_card(m) for m in out.sent)
        story = plain("\n".join(out.sent))
        assert "Entry placed" in story and "Qty 10 Entry 100.00 Stop 98.00 Target 104.00" in story
        assert "Entry filled" in story and "Qty 10 Price 100.0" in story
        assert "Protected" in story and "Stop 98.0 Target 104.0" in story
        assert "Target hit" in story
        assert "Closed" in story and "P&L +₹40" in story
        order = [
            story.index(k)
            for k in ("Entry placed", "Entry filled", "Protected", "Target hit", "Closed")
        ]
        assert order == sorted(order)

    def test_a_stop_hit_is_a_loss(self, h):
        out = start_feed(h)
        open_protected(h)
        h.tick(97.5)
        h.step()
        h.step()
        feed.poll_once(db_path(h), out)
        story = plain("\n".join(out.sent))
        assert "Stop hit" in story and "P&L -₹25" in story and "🔴 INFY · Closed" in story

    def test_an_unprotected_exit_raises_the_alarm(self, h):
        out = start_feed(h)
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.tick(97.0)  # stop can't be placed through the market
        h.sup.reconcile()
        feed.poll_once(db_path(h), out)
        assert any(
            "🚨 INFY · UNPROTECTED" in plain(m) and "exiting at market" in m for m in out.sent
        )

    def test_an_expired_entry_is_reported(self, h):
        from datetime import timedelta

        out = start_feed(h)
        h.tick(100.5)
        h.sup.open_trade(_req(entry=100.0))
        h.clock.now += timedelta(minutes=31)
        h.step()
        feed.poll_once(db_path(h), out)
        assert any("Entry cancelled" in m and "<blockquote>expired<" in m for m in out.sent)

    def test_order_noise_is_not_sent(self, h):
        out = start_feed(h)
        open_protected(h)
        feed.poll_once(db_path(h), out)
        assert all("order" not in m.lower() or "rejected" in m for m in out.sent)
        assert len(out.sent) == 3  # placed, filled, protected: not one per order change

    def test_a_trailed_stop_says_where_it_went(self, h):
        """#122: the message read payload keys trailing.py never writes ("stop raised to
        None"). Fill 100, stop 98, ATR 2: LTP 102 = 1R -> breakeven stop 100.00."""
        from executor.trade_lifecycle import TradeSupervisor

        h.sup = TradeSupervisor(
            h.db, h.om, h.pm, h.alerter, clock=h.clock, price=lambda s: h.feed.latest(s).ltp
        )
        out = start_feed(h)
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.sup.reconcile()
        h.tick(102.0)
        h.sup.reconcile()
        feed.poll_once(db_path(h), out)
        (msg,) = [plain(m) for m in out.sent if "Stop raised" in m]
        assert msg.startswith("⬆️ INFY · Stop raised") and msg.endswith("New stop 100.0")

    def test_html_in_symbols_is_escaped(self, h):
        out = start_feed(h)
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.db.execute("UPDATE events SET symbol='A<B>' WHERE entity_type='trade'")
        h.db.commit()
        feed.poll_once(db_path(h), out)
        assert any("A&lt;B&gt; · Entry placed" in m for m in out.sent)


class TestCursor:
    def test_first_run_does_not_replay_history(self, h):
        open_protected(h)  # events exist before the feed ever ran
        out = Outbox()
        assert feed.poll_once(db_path(h), out) == 0 and out.sent == []

    def test_a_restart_neither_repeats_nor_skips(self, h):
        out = start_feed(h)
        h.tick(100.0)
        h.sup.open_trade(_req())
        feed.poll_once(db_path(h), out)
        first = list(out.sent)
        assert feed.poll_once(db_path(h), out) == 0  # nothing new: nothing repeated
        h.sup.reconcile()
        feed.poll_once(db_path(h), out)  # a "restarted" feed reads the stored cursor
        assert out.sent[: len(first)] == first
        assert any("Entry filled" in m for m in out.sent[len(first) :])
        assert sum("Entry placed" in m for m in out.sent) == 1

    def test_a_telegram_outage_delays_instead_of_dropping(self, h):
        start_feed(h)
        h.tick(100.0)
        h.sup.open_trade(_req())
        down = Outbox(ok=False)
        assert feed.poll_once(db_path(h), down) == 0
        up = Outbox()
        feed.poll_once(db_path(h), up)
        assert any("Entry placed" in m for m in up.sent)

    def test_a_big_backlog_is_drained_in_batches(self, h, monkeypatch):
        monkeypatch.setattr(feed, "BATCH", 3)
        out = start_feed(h)
        for _ in range(3):
            h.tick(100.0)
            h.sup.open_trade(_req())
        feed.poll_once(db_path(h), out)
        assert sum("Entry placed" in m for m in out.sent) == 3


class TestSessionMessages:
    def day(self, tmp_path):
        from tests.executor.test_session_controller import Day

        return Day(tmp_path, harness_cls=Hostile)

    def test_halt_resume_and_close_are_announced_with_an_eod_summary(self, tmp_path):
        d = self.day(tmp_path)
        out = start_feed(d.h)
        d.trading()
        d.h.sup.open_trade(_req())
        d.at(10, 0)
        d.at(10, 1, ltp=104.2)
        d.at(10, 2, ltp=104.2)
        d.bot_mode = "paused"
        d.at(10, 3)
        d.bot_mode = "auto"
        d.at(10, 4)
        d.at(15, 14)
        feed.poll_once(db_path(d.h), out)
        assert all(is_card(m) for m in out.sent)  # session events and the EOD summary
        story = plain("\n".join(out.sent))
        assert "Market open" in story
        assert "Session HALTED" in story and "paused → /halt" in story
        assert "Session resumed" in story
        assert "Session squaring off" in story or "Session closed" in story
        assert "Session closed" in story and "flat" in story
        assert "End of day" in story and "🟢 +₹40 realised" in story
        assert sum("End of day" in m for m in out.sent) == 1

    def test_emergency_stop_says_so(self, tmp_path):
        d = self.day(tmp_path)
        out = start_feed(d.h)
        d.trading()
        d.bot_mode = "emergency_stop"
        d.at(10, 0)
        feed.poll_once(db_path(d.h), out)
        assert any("EMERGENCY STOP" in m for m in out.sent)

    def test_reconcile_mismatch_points_to_halt_and_ack(self, tmp_path):
        d = self.day(tmp_path)
        d.trading()
        d.at(9, 16)
        d.at(9, 18, fresh=False)
        out = start_feed(d.h)
        d.broker_trade(__import__("executor.models", fromlist=["OrderSide"]).OrderSide.BUY)
        d.at(9, 20)
        feed.poll_once(db_path(d.h), out)
        assert any("reconcile mismatch" in m and "/ack" in m for m in out.sent)


class TestEodSummary:
    def test_numbers_come_from_the_trade_table(self, h):
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.sup.reconcile()
        h.tick(104.2)
        h.step()
        h.step()
        text = feed.eod_summary(db_path(h), h.clock.now.date().isoformat())
        text = plain(text)
        assert "Trades 1 Closed 1 (1 won, 0 lost)" in text
        assert "+₹40 realised" in text
        assert "target_hit, +₹40" in text
        assert "Forced exits (no stop) 0" in text

    def test_forced_exits_are_counted(self, h):
        h.tick(100.0)
        h.sup.open_trade(_req())
        h.tick(97.0)
        h.sup.reconcile()
        h.sup.reconcile()
        text = feed.eod_summary(db_path(h), h.clock.now.date().isoformat())
        assert (
            "🚨 Exits forced by a missing stop: 1" in text
            and "Forced exits (no stop) 1" in plain(text)
        )

    def test_an_empty_day(self, h):
        text = feed.eod_summary(db_path(h), "2026-11-19")
        assert "Trades 0 Closed 0 (0 won, 0 lost)" in plain(text) and "🟢 ₹0 realised" in plain(
            text
        )


def test_render_ignores_non_transitions():
    row = {"kind": "heartbeat", "payload": "{}", "name": "x", "entity_type": "x", "symbol": None}
    assert feed.render(row) is None
