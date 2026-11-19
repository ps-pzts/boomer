"""Backtest acceptance gate and Sharpe ratio (split out of simulation.py, #86)."""

from __future__ import annotations

import math

from backtester.models import BacktestConfig, BacktestTrade


def check(
    cfg: BacktestConfig,
    sharpe: float,
    max_dd: float,
    trades_by_track: dict[str, int],
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    in_sample_sharpe: float | None = None,
) -> tuple[list[str], bool]:
    failures = []

    if sharpe < cfg.min_sharpe:
        failures.append(f"Sharpe {sharpe:.2f} < {cfg.min_sharpe}")

    if max_dd > cfg.max_drawdown_pct:
        failures.append(f"Max DD {max_dd:.1f}% > {cfg.max_drawdown_pct}%")

    for track, count in trades_by_track.items():
        if count < cfg.min_trades_per_track:
            failures.append(f"{track} trades {count} < {cfg.min_trades_per_track}")

    ev_ratio = (win_rate * avg_win) / max(1e-9, (1 - win_rate) * avg_loss)
    if ev_ratio < cfg.min_expectancy_ratio:
        failures.append(f"EV ratio {ev_ratio:.2f} < {cfg.min_expectancy_ratio}")

    # OOS check: only evaluated when an in-sample Sharpe is provided (walk-forward run)
    if in_sample_sharpe is not None:
        min_oos = in_sample_sharpe * cfg.min_oos_pct_of_is
        if sharpe < min_oos:
            failures.append(
                f"OOS Sharpe {sharpe:.2f} < {min_oos:.2f}"
                f" ({cfg.min_oos_pct_of_is:.0%} of IS {in_sample_sharpe:.2f})"
            )

    return failures, len(failures) == 0


def sharpe(daily_returns: list[float], risk_free_annual: float = 0.065) -> float:
    if len(daily_returns) < 2:
        return 0.0
    risk_free_daily = (1 + risk_free_annual) ** (1 / 252) - 1
    excess = [r - risk_free_daily for r in daily_returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(variance) if variance > 0 else 1e-9
    return (mean / std) * math.sqrt(252)


def daily_returns(capitals: list[float]) -> list[float]:
    return [(c - p) / p for p, c in zip(capitals, capitals[1:], strict=False) if p > 0]


def trade_stats(
    trades: list[BacktestTrade], initial_capital: float
) -> tuple[float, float, float, float]:
    """(win_rate, avg_win_pct, avg_loss_pct, expectancy) over closed trades."""
    winners = [t for t in trades if t.net_pnl > 0]
    losers = [t for t in trades if t.net_pnl <= 0]
    win_rate = len(winners) / max(1, len(trades))
    avg_win = (
        sum(t.net_pnl for t in winners) / len(winners) / initial_capital * 100 if winners else 0.0
    )
    avg_loss = (
        sum(abs(t.net_pnl) for t in losers) / len(losers) / initial_capital * 100 if losers else 0.0
    )
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    return win_rate, avg_win, avg_loss, expectancy
