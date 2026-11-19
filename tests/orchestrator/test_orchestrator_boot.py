"""Tests for orchestrator boot helpers: market feed construction and mode logging."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from src.collector.live_bars import SessionBarRecorder
from src.executor.brokers.feeds.kite_feed import KiteFeed
from src.executor.brokers.feeds.manual_feed import ManualFeed
from src.executor.brokers.kite_broker import KiteBroker
from src.orchestrator.orchestrator import _build_bar_recorder, _build_feed, _log_trading_mode


def _paper() -> KiteBroker:
    """A paper-labelled KiteBroker (#86): never the one the feed is built from."""
    from src.executor.models import BrokerName

    return KiteBroker(kite=object(), broker_name=BrokerName.PAPER)


def test_build_feed_attaches_kite_feed_sharing_instrument_cache():
    kite = KiteBroker()
    feed = _build_feed([_paper(), kite])
    assert isinstance(feed, KiteFeed)
    assert kite._feed is feed
    assert feed._instruments is kite.instruments
    assert feed.health().connected is False  # connected later, after token refresh


def test_build_feed_without_kite_returns_none():
    assert _build_feed([_paper()]) is None
    assert _build_feed([]) is None


def test_boot_logs_paper_when_env_unset(tmp_path: Path, monkeypatch, caplog):
    db = tmp_path / "b.db"
    conn = sqlite3.connect(db)
    conn.executescript(Path("migrations/0008_live_trading_authorisation.sql").read_text())
    conn.close()
    monkeypatch.delenv("BOOMER_TRADING_MODE", raising=False)
    with caplog.at_level(logging.INFO):
        _log_trading_mode(str(db))
    assert "boot_trading_mode mode=paper" in caplog.text


def test_bar_recorder_receives_every_feed_tick(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.executor.brokers.feeds.base import Tick

    monkeypatch.setenv("BOOMER_MINUTE_BARS_DIR", str(tmp_path))
    feed = ManualFeed()
    rec = _build_bar_recorder(feed)
    assert isinstance(rec, SessionBarRecorder)
    ts = datetime(2026, 11, 19, 9, 20, tzinfo=ZoneInfo("Asia/Kolkata"))
    rec.start_session(ts.date())
    feed.push(Tick(symbol="INFY", exchange="NSE", ltp=1500.0, received_at=ts))
    assert len(rec.store.bars("INFY")) == 1


def test_no_bar_recorder_without_feed():
    assert _build_bar_recorder(None) is None


def test_event_stream_started_with_mode_and_feed_health(tmp_path, monkeypatch):
    import json
    import time

    from observability import emit as obs
    from src.orchestrator.orchestrator import _start_event_stream

    monkeypatch.setattr(obs, "_current", obs._current)  # restore global after the test
    monkeypatch.setattr(obs, "HEARTBEAT_SECONDS", 0.05)
    db = tmp_path / "ev.db"
    conn = sqlite3.connect(db)
    conn.executescript(Path("migrations/0012_events.sql").read_text())
    conn.close()

    feed = ManualFeed()
    emitter = _start_event_stream(str(db), "paper", feed)
    assert obs.get() is emitter
    rows = []
    deadline = time.time() + 5
    while time.time() < deadline and not any(r[0] == "heartbeat" for r in rows):
        rows = sqlite3.connect(db).execute("SELECT kind, mode, payload FROM events").fetchall()
        time.sleep(0.05)
    emitter.stop()
    kinds = [r[0] for r in rows]
    assert kinds[0] == "lifecycle" and "heartbeat" in kinds
    assert {r[1] for r in rows} == {"paper"}
    beat = json.loads(next(r[2] for r in rows if r[0] == "heartbeat"))
    assert beat["feed_connected"] is False and beat["feed_subscribed"] == 0
