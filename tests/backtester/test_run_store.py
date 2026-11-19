"""Tests for backtester/run_store.py — backtest run persistence."""

from __future__ import annotations

from datetime import date

from backtester import run_store
from backtester.models import BacktestDailyState, BacktestSummary, BacktestTrade
from tests.backtester.sim_helpers import _make_config, _make_db


def _summary(run_id: str) -> BacktestSummary:
    return BacktestSummary(
        run_id=run_id,
        name="test",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 12),
        initial_capital=100_000.0,
        final_capital=101_500.0,
        total_return_pct=1.5,
        annualised_return_pct=40.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=2.0,
        win_rate=0.5,
        avg_win_pct=1.2,
        avg_loss_pct=0.6,
        expectancy=0.3,
        total_trades=1,
        trades_by_track={"intraday": 1},
        passes_acceptance=False,
        failure_reasons=[],
    )


def _trade() -> BacktestTrade:
    return BacktestTrade(
        symbol="RELIANCE",
        track="intraday",
        side="buy",
        entry_date=date(2024, 1, 3),
        exit_date=None,
        entry_price=100.0,
        exit_price=None,
        quantity=10,
        gross_pnl=0.0,
        transaction_costs=0.0,
        slippage_cost=0.0,
        net_pnl=0.0,
        hold_days=0,
        exit_reason="forced",
        signal_confidence=0.7,
        strategy_id="ID1",
    )


def _state() -> BacktestDailyState:
    return BacktestDailyState(
        date=date(2024, 1, 3),
        total_capital=100_000.0,
        deployed_capital=1_000.0,
        cash=99_000.0,
        open_positions=1,
        drawdown_from_hwm=0.0,
        regime="bull_calm",
    )


def test_persist_run_start_writes_running_row():
    db = _make_db()
    run_store.persist_run_start(db, "r1", _make_config(), "abc123", "2024-01-02T09:00:00")
    row = db.execute("SELECT * FROM backtest_runs WHERE run_id='r1'").fetchone()
    assert row["status"] == "running"
    assert row["code_hash"] == "abc123"
    assert row["start_date"] == "2024-01-02"
    assert row["tracks"] == '["intraday"]'


def test_daily_state_row_shape():
    row = run_store.daily_state_row("r1", _state())
    assert len(row) == 9
    assert row[1:] == ("r1", "2024-01-03", 100_000.0, 1_000.0, 99_000.0, 1, 0.0, "bull_calm")


def test_persist_run_complete_writes_summary_trades_and_daily_state():
    db = _make_db()
    run_store.persist_run_start(db, "r1", _make_config(), "abc123", "2024-01-02T09:00:00")
    rows = [run_store.daily_state_row("r1", _state())]
    run_store.persist_run_complete(db, "r1", _summary("r1"), [_trade()], rows)

    run = db.execute("SELECT * FROM backtest_runs WHERE run_id='r1'").fetchone()
    assert run["status"] == "complete"
    assert run["final_capital"] == 101_500.0
    assert run["completed_at"] is not None

    trade = db.execute("SELECT * FROM backtest_trades WHERE run_id='r1'").fetchone()
    assert trade["symbol"] == "RELIANCE"
    assert trade["exit_date"] is None  # open trade stored as NULL, not "None"

    n_states = db.execute("SELECT COUNT(*) FROM backtest_daily_state WHERE run_id='r1'")
    assert n_states.fetchone()[0] == 1


def test_persist_run_complete_with_no_trades_or_states():
    db = _make_db()
    run_store.persist_run_start(db, "r2", _make_config(), "abc123", "2024-01-02T09:00:00")
    run_store.persist_run_complete(db, "r2", _summary("r2"), [], [])
    assert db.execute("SELECT status FROM backtest_runs WHERE run_id='r2'").fetchone()[0] == (
        "complete"
    )
    assert db.execute("SELECT COUNT(*) FROM backtest_trades").fetchone()[0] == 0
