"""SQLite persistence for backtest runs: run header, trades, and daily state.

Split out of simulation.py by responsibility; BacktestSimulation keeps thin
delegating methods so the simulation loop reads the same as before.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from backtester.models import (
    BacktestConfig,
    BacktestDailyState,
    BacktestSummary,
    BacktestTrade,
)

IST = ZoneInfo("Asia/Kolkata")

DailyStateRow = tuple


def persist_run_start(
    db: sqlite3.Connection,
    run_id: str,
    config: BacktestConfig,
    code_hash: str,
    start_iso: str,
) -> None:
    db.execute(
        """
        INSERT INTO backtest_runs
        (run_id, name, code_hash, start_date, end_date, initial_capital,
         final_capital, status, tracks, universe, created_at)
        VALUES (?,?,?,?,?,?,0,'running',?,?,?)
        """,
        (
            run_id,
            config.name,
            code_hash,
            str(config.start_date),
            str(config.end_date),
            config.initial_capital,
            json.dumps(config.tracks),
            config.universe,
            start_iso,
        ),
    )
    db.commit()


def persist_run_complete(
    db: sqlite3.Connection,
    run_id: str,
    summary: BacktestSummary,
    trades: list[BacktestTrade],
    daily_rows: list[DailyStateRow],
) -> None:
    now = datetime.now(IST).replace(tzinfo=None).isoformat()
    db.execute(
        """
        UPDATE backtest_runs SET
            final_capital=?, total_return_pct=?, annualised_return_pct=?,
            sharpe_ratio=?, max_drawdown_pct=?, win_rate=?, avg_win_pct=?,
            avg_loss_pct=?, expectancy=?, total_trades=?, status='complete', completed_at=?
        WHERE run_id=?
        """,
        (
            summary.final_capital,
            summary.total_return_pct,
            summary.annualised_return_pct,
            summary.sharpe_ratio,
            summary.max_drawdown_pct,
            summary.win_rate,
            summary.avg_win_pct,
            summary.avg_loss_pct,
            summary.expectancy,
            summary.total_trades,
            now,
            run_id,
        ),
    )
    for trade in trades:
        db.execute(
            """
            INSERT INTO backtest_trades
            (trade_id, run_id, symbol, track, side, entry_date, exit_date,
             entry_price, exit_price, quantity, gross_pnl, transaction_costs,
             slippage_cost, net_pnl, hold_days, exit_reason, signal_confidence, strategy_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                run_id,
                trade.symbol,
                trade.track,
                trade.side,
                str(trade.entry_date),
                str(trade.exit_date) if trade.exit_date else None,
                trade.entry_price,
                trade.exit_price,
                trade.quantity,
                trade.gross_pnl,
                trade.transaction_costs,
                trade.slippage_cost,
                trade.net_pnl,
                trade.hold_days,
                trade.exit_reason,
                trade.signal_confidence,
                trade.strategy_id,
            ),
        )
    # Flush all buffered daily states in one go
    for row in daily_rows:
        db.execute(
            """
            INSERT OR REPLACE INTO backtest_daily_state
            (state_id, run_id, date, total_capital, deployed_capital, cash,
             open_positions, drawdown_from_hwm, regime)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            row,
        )
    db.commit()


def daily_state_row(run_id: str, state: BacktestDailyState) -> DailyStateRow:
    """Build the backtest_daily_state row for one simulated day."""
    return (
        str(uuid.uuid4()),
        run_id,
        str(state.date),
        state.total_capital,
        state.deployed_capital,
        state.cash,
        state.open_positions,
        state.drawdown_from_hwm,
        state.regime,
    )


_HASHED_SOURCES = (
    "simulation.py",
    "models.py",
    "costs.py",
    "slippage.py",
    "run_store.py",
    "bar_broker.py",
    "acceptance.py",
)


def code_hash() -> str:
    """Hash the backtester source files so holdout tracking resets on code changes."""
    hasher = hashlib.sha256()
    base = pathlib.Path(__file__).parent
    for fname in sorted(_HASHED_SOURCES):
        p = base / fname
        if p.exists():
            hasher.update(p.read_bytes())
    return hasher.hexdigest()[:16]
