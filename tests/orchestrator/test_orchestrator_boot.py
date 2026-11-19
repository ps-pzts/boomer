"""Tests for orchestrator boot helpers: market feed construction and mode logging."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from src.executor.brokers.feeds.kite_feed import KiteFeed
from src.executor.brokers.kite_broker import KiteBroker
from src.executor.brokers.mock_broker import MockBroker
from src.orchestrator.orchestrator import _build_feed, _log_trading_mode


def test_build_feed_attaches_kite_feed_sharing_instrument_cache():
    kite = KiteBroker()
    feed = _build_feed([MockBroker(), kite])
    assert isinstance(feed, KiteFeed)
    assert kite._feed is feed
    assert feed._instruments is kite.instruments
    assert feed.health().connected is False  # connected later, after token refresh


def test_build_feed_without_kite_returns_none():
    assert _build_feed([MockBroker()]) is None
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
