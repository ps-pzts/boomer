"""Tests for circuit breaker evaluation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from capital.circuit_breakers import BreakerStatus, evaluate_circuit_breakers
from capital.models import RiskConfig, Track
from capital.risk_config import RiskConfigStore
from db.migrations import run_migrations

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    run_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture()
def config(db: Path) -> RiskConfig:
    store = RiskConfigStore(db)
    return store.seed_defaults(date(2026, 1, 1))


def _eval(config: RiskConfig, **overrides):
    defaults = dict(
        intraday_realised_pnl_today=Decimal("0"),
        intraday_bucket_capital=Decimal("50000"),
        intraday_consecutive_losses_today=0,
        portfolio_realised_pnl_today=Decimal("0"),
        total_capital=Decimal("50000"),
        portfolio_realised_pnl_this_week=Decimal("0"),
        live_drawdown_pct=Decimal("0"),
        nifty_intraday_move_pct=Decimal("0"),
        current_time_ist_hour=10,
        current_time_ist_minute=0,
        black_swan_manually_tripped=False,
        config=config,
    )
    defaults.update(overrides)
    return evaluate_circuit_breakers(**defaults)


def test_all_clear_by_default(config: RiskConfig) -> None:
    state = _eval(config)
    assert not state.track_blocked(Track.INTRADAY)


def test_intraday_daily_loss_trips_at_2pct(config: RiskConfig) -> None:
    # 2% of ₹50,000 bucket = ₹1,000 loss
    state = _eval(config, intraday_realised_pnl_today=Decimal("-1000"))
    assert state.intraday_daily_loss == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.INTRADAY)


def test_intraday_consecutive_losses(config: RiskConfig) -> None:
    state = _eval(config, intraday_consecutive_losses_today=3)
    assert state.intraday_consecutive_losses == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.INTRADAY)


def test_intraday_late_entry_after_1430(config: RiskConfig) -> None:
    state = _eval(config, current_time_ist_hour=14, current_time_ist_minute=30)
    assert state.intraday_late_entry == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.INTRADAY)


def test_intraday_not_blocked_at_1429(config: RiskConfig) -> None:
    state = _eval(config, current_time_ist_hour=14, current_time_ist_minute=29)
    assert state.intraday_late_entry == BreakerStatus.CLEAR


def test_portfolio_daily_loss_blocks_all_tracks(config: RiskConfig) -> None:
    # 2% of ₹50,000 = ₹1,000 portfolio loss
    state = _eval(config, portfolio_realised_pnl_today=Decimal("-1000"))
    assert state.portfolio_daily_loss == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.INTRADAY)


def test_portfolio_max_drawdown_8pct(config: RiskConfig) -> None:
    state = _eval(config, live_drawdown_pct=Decimal("0.08"))
    assert state.portfolio_max_drawdown == BreakerStatus.TRIPPED
    assert state.requires_manual_resume()
    assert state.track_blocked(Track.INTRADAY)


def test_black_swan_nifty_minus_3pct(config: RiskConfig) -> None:
    state = _eval(config, nifty_intraday_move_pct=Decimal("-0.031"))
    assert state.black_swan == BreakerStatus.TRIPPED
    assert state.requires_manual_resume()


def test_black_swan_not_tripped_at_minus_1pct(config: RiskConfig) -> None:
    state = _eval(config, nifty_intraday_move_pct=Decimal("-0.01"))
    assert state.black_swan == BreakerStatus.CLEAR


def test_black_swan_manual_trip(config: RiskConfig) -> None:
    state = _eval(config, black_swan_manually_tripped=True)
    assert state.black_swan == BreakerStatus.TRIPPED


def test_any_tripped_true_when_one_breaker_tripped(config: RiskConfig) -> None:
    state = _eval(config, black_swan_manually_tripped=True)
    assert state.any_tripped()


def test_any_tripped_false_when_all_clear(config: RiskConfig) -> None:
    state = _eval(config)
    assert not state.any_tripped()
