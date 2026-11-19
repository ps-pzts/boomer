"""#105: the live projection, timelines and event descriptions, on real paper days.

Worked numbers as the rest of the suite (qty 10, entry 100, stop 98, target 104):
a target hit is +₹40, a gapped stop (97.5) is -₹25, so the day is +₹15 realised.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from observability import describe, projection
from tests.executor.test_trade_lifecycle import _req
from tests.integration.harness import Hostile
from tests.integration.test_scenarios import open_protected

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 19, 11, 0, tzinfo=IST)  # a Thursday, inside market hours


@pytest.fixture()
def h(tmp_path) -> Hostile:
    return Hostile(tmp_path)


def path(h: Hostile) -> str:
    """The db file. Events are stamped with the wall-clock date and these tests run on a fake
    clock, so line the stamps up with the fake day (in production they are the same day)."""
    h.db.execute("UPDATE events SET session_date=?", (h.clock.now.date().isoformat(),))
    h.db.commit()
    return h.db.execute("PRAGMA database_list").fetchone()[2]


def heartbeat(h: Hostile, source: str, ago_s: float, seq: int = 1, boot: str = "b1", **p) -> None:
    ts = (NOW - timedelta(seconds=ago_s)).isoformat(timespec="milliseconds")
    h.db.execute(
        "INSERT INTO events (ts, session_date, mode, source, boot_id, source_seq, kind, name,"
        " severity, payload) VALUES (?, ?, 'paper', ?, ?, ?, 'heartbeat', 'heartbeat', 'info', ?)",
        (
            ts,
            NOW.date().isoformat(),
            source,
            boot,
            seq,
            json.dumps({"queue_depth": 0, "dropped": 0, **p}),
        ),
    )
    h.db.commit()


def two_trades(h: Hostile) -> tuple[str, str]:
    win = open_protected(h)
    h.tick(104.2)
    h.step()
    h.step()
    h.tick(100.0)
    loss = h.sup.open_trade(_req())
    h.step()
    h.tick(97.5)
    h.step()
    h.step()
    return win, loss


class TestLive:
    def test_a_day_with_a_win_and_a_loss(self, h):
        two_trades(h)
        snap = projection.live(path(h), h.clock.now)
        k = snap["kpis"]
        assert (k["realised"], k["closed"], k["wins"], k["losses"], k["active"]) == (
            15.0,
            2,
            1,
            1,
            0,
        )
        assert [p["cum"] for p in snap["pnl"]] == [40.0, 15.0]
        assert snap["session"] is None and snap["bot_mode"] == "auto"
        assert snap["drift"]["available"] is False

    def test_an_open_trade_card_has_prices_and_protection(self, h):
        open_protected(h)
        h.tick(101.5)
        h.step()
        (t,) = projection.live(path(h), h.clock.now)["trades"]
        assert (t["symbol"], t["state"], t["quantity"]) == ("INFY", "open", 10)
        assert (t["entry"], t["stop"], t["target"]) == (100.0, 98.0, 104.0)
        assert t["protected"] is True and t["unprotected"] is False

    def test_closed_trades_carry_no_live_prices(self, h):
        two_trades(h)
        for t in projection.live(path(h), h.clock.now)["trades"]:
            assert t["last"] is None and t["unrealised"] is None
            assert t["exit_reason"] in ("target_hit", "stop_hit")

    def test_stream_is_newest_first_and_leaves_out_heartbeats(self, h):
        open_protected(h)
        heartbeat(h, "orchestrator", 10)
        stream = projection.live(path(h), h.clock.now)["stream"]
        assert [e["id"] for e in stream] == sorted((e["id"] for e in stream), reverse=True)
        assert all(e["kind"] != "heartbeat" for e in stream)
        assert stream[0]["title"] == "Stop confirmed at the broker"

    def test_stream_is_capped(self, h, monkeypatch):
        monkeypatch.setattr(projection, "STREAM_LIMIT", 3)
        two_trades(h)
        assert len(projection.live(path(h), h.clock.now)["stream"]) == 3

    def test_the_view_cannot_write(self, h):
        c = projection.connect(path(h))
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("DELETE FROM events")
        c.close()


class TestComponents:
    def test_fresh_heartbeat_is_alive(self, h):
        heartbeat(h, "orchestrator", 20)
        (c,) = projection.live(path(h), NOW)["components"]
        assert (c["source"], c["status"], c["age_s"]) == ("orchestrator", "alive", 20)

    def test_three_missed_beats_in_market_hours_is_silent(self, h):
        heartbeat(h, "orchestrator", 95)
        snap = projection.live(path(h), NOW)
        assert snap["components"][0]["status"] == "silent"
        assert any("orchestrator silent for 95s" in a["text"] for a in snap["attention"])

    def test_boundary_is_ninety_seconds(self, h):
        heartbeat(h, "a", 90)
        heartbeat(h, "b", 91)
        by = {c["source"]: c["status"] for c in projection.live(path(h), NOW)["components"]}
        assert by == {"a": "alive", "b": "silent"}

    def test_quiet_outside_market_hours_is_idle_not_silent(self, h):
        heartbeat(h, "orchestrator", 3600)
        night = NOW.replace(hour=20)
        c = projection.live(path(h), night + timedelta(seconds=0))["components"][0]
        assert c["status"] == "idle"

    def test_weekend_is_idle(self, h):
        heartbeat(h, "orchestrator", 3600)
        saturday = NOW + timedelta(days=2)
        assert projection.live(path(h), saturday)["components"][0]["status"] == "idle"

    def test_a_gap_in_the_event_sequence_is_reported(self, h):
        heartbeat(h, "feed", 10, seq=1)
        heartbeat(h, "feed", 5, seq=4)  # 2 and 3 never arrived
        snap = projection.live(path(h), NOW)
        assert snap["components"][0]["seq_gap"] == 2
        assert any("feed lost 2 events" in a["text"] for a in snap["attention"])

    def test_dropped_events_are_reported(self, h):
        heartbeat(h, "orchestrator", 10, dropped=7)
        assert any("dropped 7" in a["text"] for a in projection.live(path(h), NOW)["attention"])

    def test_the_newest_heartbeat_wins_and_health_is_passed_through(self, h):
        heartbeat(h, "orchestrator", 60, seq=1, feed_connected=False)
        heartbeat(h, "orchestrator", 5, seq=2, feed_connected=True)
        (c,) = projection.live(path(h), NOW)["components"]
        assert c["age_s"] == 5 and c["health"] == {"feed_connected": True}


class TestAttention:
    def test_a_halted_session_leads_the_list(self, h):
        h.db.execute(
            "INSERT INTO trading_sessions (session_date, mode, state, halt_reason, halt_detail,"
            " created_at, updated_at)"
            " VALUES (?, 'paper', 'halted', 'reconcile_mismatch', ?, 't', 't')",
            (
                NOW.date().isoformat(),
                json.dumps({"failed_checks": ["net_quantity_matches_broker"]}),
            ),
        )
        h.db.commit()
        heartbeat(h, "orchestrator", 200)
        snap = projection.live(path(h), NOW)
        assert snap["attention"][0] == {
            "severity": "critical", "text": "Session halted: reconcile_mismatch",
            "kind": "session", "ref": None, "ts": None,
        }  # fmt: skip
        assert snap["session"]["halt_detail"]["failed_checks"] == ["net_quantity_matches_broker"]
        assert [a["severity"] for a in snap["attention"]] == ["critical", "warn"]

    def test_an_unprotected_position_is_critical(self, h):
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.step()
        h.db.execute("UPDATE positions SET unprotected_flag=1")
        h.db.commit()
        items = projection.live(path(h), h.clock.now)["attention"]
        assert any(
            i["severity"] == "critical" and "no confirmed stop" in i["text"] and i["ref"] == bid
            for i in items
        )

    def test_a_trade_stuck_in_protecting_is_critical(self, h):
        open_protected(h)
        stale = (h.clock.now - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        h.db.execute("UPDATE intraday_brackets SET state='protecting', updated_at=?", (stale,))
        h.db.commit()
        items = projection.live(path(h), h.clock.now)["attention"]
        assert any("stuck in protecting" in i["text"] for i in items)

    def test_quiet_system_has_nothing_to_flag(self, h):
        open_protected(h)
        heartbeat(h, "orchestrator", 5)
        assert projection.live(path(h), NOW)["attention"] == []


class TestFingerprint:
    def test_unchanged_data_has_a_stable_fingerprint_despite_the_clock(self, h):
        open_protected(h)
        heartbeat(h, "orchestrator", 5)
        a = projection.fingerprint(projection.live(path(h), NOW))
        b = projection.fingerprint(projection.live(path(h), NOW + timedelta(seconds=40)))
        assert a == b

    def test_a_new_event_changes_it(self, h):
        open_protected(h)
        a = projection.fingerprint(projection.live(path(h), h.clock.now))
        h.tick(104.2)
        h.step()
        assert projection.fingerprint(projection.live(path(h), h.clock.now)) != a


class TestTimelines:
    def test_a_trade_timeline_tells_the_story_in_order(self, h):
        win, _ = two_trades(h)
        t = projection.trade_timeline(path(h), win)
        names = [e["name"] for e in t["events"] if e["entity_type"] == "trade"]
        assert names == ["created", "entry_filled", "stop_confirmed", "leg_filled", "flat"]
        assert [e["id"] for e in t["events"]] == sorted(e["id"] for e in t["events"])
        assert t["trade"]["exit_reason"] == "target_hit" and t["trade"]["realised_pnl"] == 40.0
        assert {o["side"] for o in t["orders"]} == {"buy", "sell"}
        flat = [e for e in t["events"] if e["name"] == "flat"][0]
        assert flat["from_state"] == "exiting" and flat["to_state"] == "closed"
        assert flat["payload"]["rule"] == "net_position_zero"

    def test_the_timeline_includes_its_orders_events_and_no_other_trades(self, h):
        win, loss = two_trades(h)
        win_ids = {e["id"] for e in projection.trade_timeline(path(h), win)["events"]}
        loss_ids = {e["id"] for e in projection.trade_timeline(path(h), loss)["events"]}
        assert win_ids and loss_ids and not (win_ids & loss_ids)
        kinds = {e["entity_type"] for e in projection.trade_timeline(path(h), win)["events"]}
        assert kinds == {"trade", "order"}

    def test_unknown_trade(self, h):
        assert projection.trade_timeline(path(h), "nope") is None

    def test_a_session_timeline_lists_trades_and_routine_free_events(self, h):
        two_trades(h)
        heartbeat(h, "orchestrator", 5)
        day = h.clock.now.date().isoformat()
        t = projection.session_timeline(path(h), day)
        assert len(t["trades"]) == 2 and t["events"]
        assert all(e["kind"] != "heartbeat" for e in t["events"])

    def test_days_sum_the_results(self, h):
        two_trades(h)
        (d,) = projection.days(path(h))
        assert (d["day"], d["trades"], d["closed"], d["realised"], d["wins"], d["losses"]) == (
            h.clock.now.date().isoformat(), 2, 2, 15.0, 1, 1,
        )  # fmt: skip

    def test_days_with_no_trades(self, h):
        assert projection.days(path(h)) == []

    @pytest.mark.parametrize("n, expected", [(0, 1), (500, 90)])
    def test_days_limit_is_clamped(self, h, n, expected):
        assert projection.days(path(h), n) == []


class TestDescribe:
    def row(self, **kw) -> dict:
        base = dict(event_id=1, ts="t", kind="state_transition", name="flat", severity="info",
                    entity_type="trade", entity_id="x", symbol="INFY", trade_id="x",
                    source="trade", payload="{}")  # fmt: skip
        return base | kw

    def test_titles(self):
        assert describe.title(self.row()) == "Trade closed"
        assert describe.title(self.row(entity_type="session", name="halt")) == "Session halted"
        assert describe.title(self.row(entity_type="order", name="rejected")) == "Order rejected"
        assert describe.title(self.row(kind="decision", name="trade_opened")) == "Trade opened"
        assert (
            describe.title(
                self.row(kind="transition_blocked", name="market_open", entity_type="session")
            )
            == "session blocked: market open"
        )

    def test_unknown_names_fall_back_to_readable_text(self):
        assert describe.title(self.row(name="some_new_thing")) == "some new thing"

    def test_detail_shows_the_rule_and_headline_numbers(self):
        p = json.dumps({"rule": "net_position_zero", "inputs": {"realised_pnl": 40.0, "junk": 1}})
        assert describe.detail(self.row(payload=p)) == "rule net_position_zero, realised_pnl 40.0"

    def test_bad_payload_is_tolerated(self):
        assert describe.payload(self.row(payload="{not json")) == {}
        assert describe.detail(self.row(payload=None)) == ""

    @pytest.mark.parametrize(
        "kw, expected",
        [
            ({}, "trade"),
            ({"entity_type": "session"}, "session"),
            ({"severity": "critical"}, "alert"),
            ({"kind": "error"}, "alert"),
            ({"entity_type": "order"}, "other"),
        ],
    )
    def test_groups(self, kw, expected):
        assert describe.group(self.row(**kw)) == expected
