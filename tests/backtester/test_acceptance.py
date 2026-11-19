"""Backtest statistics (backtester/acceptance.py, split out of simulation.py in #86)."""

from __future__ import annotations

from datetime import date

import pytest

from backtester import acceptance, run_store
from backtester.models import BacktestTrade


def _trade(net: float) -> BacktestTrade:
    d = date(2024, 1, 2)
    return BacktestTrade(
        "X", "intraday", "long", d, d, 100.0, 100.0, 1, net, 0.0, 0.0, net, 0, "sl", 0.5, "s"
    )


def test_daily_returns():
    assert acceptance.daily_returns([100.0, 110.0, 99.0]) == pytest.approx([0.10, -0.10])
    assert acceptance.daily_returns([0.0, 10.0]) == []  # no return from a zero base
    assert acceptance.daily_returns([]) == []


def test_trade_stats_worked_example():
    """Two winners (+300, +100) and one loser (-200) on 10,000 capital:
    win rate 2/3, avg win 2%, avg loss 2%, expectancy 2/3*2 - 1/3*2 = 0.6667."""
    win_rate, avg_win, avg_loss, ev = acceptance.trade_stats(
        [_trade(300.0), _trade(100.0), _trade(-200.0)], 10_000.0
    )
    assert win_rate == pytest.approx(2 / 3)
    assert avg_win == pytest.approx(2.0) and avg_loss == pytest.approx(2.0)
    assert ev == pytest.approx(2 / 3 * 2 - 1 / 3 * 2)


def test_trade_stats_no_trades():
    assert acceptance.trade_stats([], 10_000.0) == (0.0, 0.0, 0.0, 0.0)


def test_sharpe_needs_two_returns():
    assert acceptance.sharpe([0.01]) == 0.0
    assert acceptance.sharpe([0.01, 0.012] * 25) > 1.0


def test_code_hash_covers_the_new_files():
    assert "bar_broker.py" in run_store._HASHED_SOURCES
    assert "acceptance.py" in run_store._HASHED_SOURCES
    assert len(run_store.code_hash()) == 16
