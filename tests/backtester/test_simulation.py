from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from backtester.models import BacktestConfig
from backtester.simulation import BacktestSimulation
from executor.models import PriceBar


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    return db


def _flat_price_loader(symbol: str, from_date: str, to_date: str) -> list[PriceBar]:
    """Returns a flat ₹100 bar for any symbol/date (deterministic)."""
    return [
        PriceBar(
            symbol=symbol, date=from_date, open=100, high=102, low=98, close=100, volume=100_000
        )
    ]


def _empty_feature_loader(symbol: str, as_of_date: str) -> dict:
    return {}


class TestBacktestSimulationBasic:
    def test_run_completes_and_persists_run(self):
        db = _make_db()
        config = BacktestConfig(
            name="test_run",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            initial_capital=100_000.0,
            tracks=["swing"],
        )
        sim = BacktestSimulation(
            db=db,
            config=config,
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE", "TCS"],
        )
        summary = sim.run()
        assert summary.run_id is not None
        row = db.execute(
            "SELECT status FROM backtest_runs WHERE run_id=?", (summary.run_id,)
        ).fetchone()
        assert row["status"] == "complete"

    def test_daily_states_persisted(self):
        db = _make_db()
        config = BacktestConfig(
            name="daily_test",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            initial_capital=50_000.0,
            tracks=["swing"],
        )
        sim = BacktestSimulation(
            db=db,
            config=config,
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=["TCS"],
        )
        summary = sim.run()
        count = db.execute(
            "SELECT COUNT(*) FROM backtest_daily_state WHERE run_id=?", (summary.run_id,)
        ).fetchone()[0]
        assert count >= 2  # at least 2 trading days in Jan 2-5

    def test_capital_stays_near_initial_with_no_trades(self):
        db = _make_db()
        config = BacktestConfig(
            name="no_trades",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            initial_capital=100_000.0,
            tracks=["swing"],
        )
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=["RELIANCE"],
        )
        summary = sim.run()
        assert summary.final_capital == pytest.approx(100_000.0, rel=0.01)
        assert summary.total_trades == 0


class TestBacktestSharpe:
    def test_sharpe_with_all_positive_returns(self):
        from backtester.simulation import BacktestSimulation
        all_up = [0.01] * 100   # 1% daily return consistently
        sharpe = BacktestSimulation._sharpe(all_up)
        assert sharpe > 1.0

    def test_sharpe_with_empty_returns_is_zero(self):
        sharpe = BacktestSimulation._sharpe([])
        assert sharpe == pytest.approx(0.0)

    def test_sharpe_with_single_return_is_zero(self):
        sharpe = BacktestSimulation._sharpe([0.01])
        assert sharpe == pytest.approx(0.0)


class TestBacktestTradingDays:
    def test_weekdays_only(self):
        db = _make_db()
        config = BacktestConfig(
            name="x", start_date=date(2024, 1, 1), end_date=date(2024, 1, 7),
            initial_capital=100_000.0,
        )
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        days = list(sim._trading_days())
        # Jan 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat(skip), 7=Sun(skip)
        assert all(d.weekday() < 5 for d in days)
        assert len(days) == 5


class TestBacktestAcceptance:
    def test_acceptance_fails_on_low_sharpe(self):
        db = _make_db()
        config = BacktestConfig(
            name="x", start_date=date(2024, 1, 2), end_date=date(2024, 1, 5),
            initial_capital=100_000.0,
        )
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=0.5, max_dd=5.0,
            trades_by_track={"long_term": 200, "swing": 200, "intraday": 200},
            expectancy=0.5, win_rate=0.6, avg_win=2.0, avg_loss=1.0,
        )
        assert not passes
        assert any("Sharpe" in f for f in failures)

    def test_acceptance_passes_all_criteria(self):
        db = _make_db()
        config = BacktestConfig(
            name="x", start_date=date(2024, 1, 2), end_date=date(2024, 1, 5),
            initial_capital=100_000.0,
        )
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5, max_dd=10.0,
            trades_by_track={"long_term": 150, "swing": 150, "intraday": 150},
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
        )
        assert passes
        assert failures == []

    def test_acceptance_fails_on_too_few_trades(self):
        db = _make_db()
        config = BacktestConfig(
            name="x", start_date=date(2024, 1, 2), end_date=date(2024, 1, 5),
            initial_capital=100_000.0,
        )
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5, max_dd=10.0,
            trades_by_track={"swing": 50},  # < 100 minimum
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
        )
        assert not passes
        assert any("trades" in f for f in failures)
