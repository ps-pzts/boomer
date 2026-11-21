"""Tests for observability/emit.py — the event emitter (#56)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from observability import emit as obs
from observability.emit import Emitter

IST = ZoneInfo("Asia/Kolkata")
EVENTS_SQL = (Path(__file__).parents[2] / "migrations" / "0012_events.sql").read_text()


@pytest.fixture()
def db(tmp_path) -> str:
    p = str(tmp_path / "e.db")
    c = sqlite3.connect(p)
    c.executescript(EVENTS_SQL)
    c.close()
    return p


def _rows(db: str) -> list[sqlite3.Row]:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c.execute("SELECT * FROM events ORDER BY event_id").fetchall()


def _wait_for(db: str, n: int, timeout: float = 5.0) -> list[sqlite3.Row]:
    end = time.time() + timeout
    while time.time() < end:
        rows = _rows(db)
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    return _rows(db)


class TestEnvelope:
    def test_fields_and_ist_timestamp(self):
        fixed = datetime(2026, 11, 19, 10, 0, 0, 123000, tzinfo=IST)
        e = Emitter(None, "feed", mode="paper", clock=lambda: fixed)
        row = e.envelope("metric", "ticks", symbol="INFY", trade_id="t1", count=42)
        (ts, session_date, mode, source, boot_id, seq, kind, name, severity, *_rest) = row
        assert ts == "2026-11-19T10:00:00.123+05:30"
        assert (session_date, mode, source, seq, kind, name, severity) == (
            "2026-11-19",
            "paper",
            "feed",
            1,
            "metric",
            "ticks",
            "info",
        )
        assert boot_id == e.boot_id
        assert json.loads(row[15]) == {"count": 42}

    def test_seq_monotonic_per_source(self):
        e = Emitter(None, "orchestrator")
        seqs = [e.envelope("k", "n")[5] for _ in range(3)]
        other = e.envelope("k", "n", source="order")[5]
        assert seqs == [1, 2, 3]
        assert other == 1  # separate counter per source

    def test_secrets_redacted(self):
        e = Emitter(None, "x")
        payload = json.loads(e.envelope("k", "n", access_token="abc", KITE_API_KEY="k", qty=5)[15])
        assert payload == {"access_token": "[redacted]", "KITE_API_KEY": "[redacted]", "qty": 5}

    def test_non_json_values_serialised(self):
        from decimal import Decimal

        e = Emitter(None, "x")
        assert json.loads(e.envelope("k", "n", price=Decimal("1.5"))[15]) == {"price": "1.5"}


class TestAsyncPath:
    def test_writer_persists_lifecycle_and_events(self, db):
        e = Emitter(db, "orchestrator", mode="paper")
        e.start(heartbeat_seconds=None)
        e.emit("decision", "watchlist_selected", size=50)
        rows = _wait_for(db, 2)
        e.stop()
        assert [(r["kind"], r["name"]) for r in rows[:2]] == [
            ("lifecycle", "started"),
            ("decision", "watchlist_selected"),
        ]
        assert [r["source_seq"] for r in rows[:2]] == [1, 2]
        assert _rows(db)[-1]["name"] == "stopped"  # stop() flushes its own event

    def test_heartbeat_carries_stats_and_health(self, db):
        e = Emitter(db, "orchestrator")
        e.start(heartbeat_seconds=0.05, health=lambda: {"feed_connected": True})
        rows = [r for r in _wait_for(db, 3) if r["kind"] == "heartbeat"]
        e.stop()
        assert rows
        payload = json.loads(rows[0]["payload"])
        assert payload["feed_connected"] is True
        assert {"queue_depth", "dropped", "written", "write_errors"} <= payload.keys()

    def test_failing_health_callback_still_beats(self, db):
        def boom() -> dict:
            raise RuntimeError("feed gone")

        e = Emitter(db, "orchestrator")
        e.start(heartbeat_seconds=0.05, health=boom)
        rows = [r for r in _wait_for(db, 2) if r["kind"] == "heartbeat"]
        e.stop()
        assert json.loads(rows[0]["payload"])["health_error"] == "feed gone"

    def test_emit_never_raises_and_counts_drops_when_full(self, monkeypatch):
        monkeypatch.setattr(obs, "QUEUE_MAX", 2)
        e = Emitter(None, "x")  # not started: nothing drains the queue
        for _ in range(5):
            e.emit("k", "n")
        assert e.stats()["dropped"] == 3
        assert e.stats()["queue_depth"] == 2

    def test_emit_survives_unserialisable_payload_bug(self):
        e = Emitter(None, "x")

        class Weird:
            def __str__(self) -> str:
                raise ValueError("no")

        e.emit("k", "n", thing=Weird())  # must not raise
        assert e.stats()["dropped"] == 1

    def test_write_failure_counted_not_raised(self, tmp_path):
        e = Emitter(str(tmp_path / "no_events_table.db"), "x")
        e.start(heartbeat_seconds=None)
        e.emit("k", "n")
        deadline = time.time() + 5
        while e.stats()["write_errors"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        e.stop()
        assert e.stats()["write_errors"] >= 1

    def test_unconfigured_module_emit_is_a_counted_noop(self):
        obs.emit("k", "n")  # default emitter has no db: must not raise
