"""Tests for executor/trading_mode.py — the paper/live switch fails closed."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.mock_broker import MockBroker
from executor.brokers.paper_broker import PaperBroker
from executor.trading_mode import (
    LiveTradingAuthorisationStore,
    TradingMode,
    build_execution_broker,
    resolve_trading_mode,
)


@pytest.fixture()
def store(tmp_path: Path) -> LiveTradingAuthorisationStore:
    db = tmp_path / "boomer.db"
    conn = sqlite3.connect(db)
    conn.executescript(Path("migrations/0008_live_trading_authorisation.sql").read_text())
    conn.close()
    return LiveTradingAuthorisationStore(db)


class TestAuthorisationStore:
    def test_no_rows_means_disabled(self, store):
        assert store.is_enabled() is False

    def test_latest_row_wins(self, store):
        store.set_enabled(True, actor="operator", reason="Q5 criteria met")
        assert store.is_enabled() is True
        store.set_enabled(False, actor="operator", reason="drawdown review")
        assert store.is_enabled() is False

    def test_actor_and_reason_required(self, store):
        with pytest.raises(ValueError):
            store.set_enabled(True, actor="", reason="x")
        with pytest.raises(ValueError):
            store.set_enabled(True, actor="operator", reason="")


class TestResolveTradingMode:
    @pytest.mark.parametrize("env", [None, "", "paper", "LIVE ", "Live", "liv", "production"])
    def test_env_not_live_is_paper_even_when_authorised(self, store, env):
        store.set_enabled(True, actor="operator", reason="test")
        expected = TradingMode.LIVE if (env or "").strip().lower() == "live" else TradingMode.PAPER
        assert resolve_trading_mode(env, store).mode == expected

    def test_env_live_without_authorisation_is_paper(self, store):
        decision = resolve_trading_mode("live", store)
        assert decision.mode == TradingMode.PAPER
        assert "not enabled" in decision.reason

    def test_env_live_with_authorisation_is_live(self, store):
        store.set_enabled(True, actor="operator", reason="test")
        assert resolve_trading_mode("live", store).mode == TradingMode.LIVE

    def test_missing_table_fails_closed_to_paper(self, tmp_path):
        bare = LiveTradingAuthorisationStore(tmp_path / "empty.db")
        decision = resolve_trading_mode("live", bare)
        assert decision.mode == TradingMode.PAPER
        assert "failed" in decision.reason


class TestBuildExecutionBroker:
    def test_paper_builds_paper_broker_on_feed(self):
        broker = build_execution_broker(TradingMode.PAPER, ManualFeed(), None, 250_000.0)
        assert isinstance(broker, PaperBroker)
        assert broker.get_funds().available_cash == pytest.approx(250_000.0)

    def test_paper_never_returns_live_broker(self):
        live = MockBroker()
        broker = build_execution_broker(TradingMode.PAPER, ManualFeed(), live, 1.0)
        assert broker is not live

    def test_paper_requires_positive_cash(self):
        with pytest.raises(ValueError):
            build_execution_broker(TradingMode.PAPER, ManualFeed(), None, 0.0)

    def test_live_returns_live_broker(self):
        live = MockBroker()
        assert build_execution_broker(TradingMode.LIVE, ManualFeed(), live, 1.0) is live

    def test_live_without_broker_raises(self):
        with pytest.raises(RuntimeError):
            build_execution_broker(TradingMode.LIVE, ManualFeed(), None, 1.0)
