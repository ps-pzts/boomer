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
        intraday_bucket_capital=Decimal("2500"),
        intraday_consecutive_losses_today=0,
        swing_realised_pnl_this_week=Decimal("0"),
        swing_bucket_capital=Decimal("7500"),
        swing_losing_trades_30d=0,
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
    assert not state.track_blocked(Track.SWING)
    assert not state.track_blocked(Track.LONG_TERM)


def test_intraday_daily_loss_trips_at_2pct(config: RiskConfig) -> None:
    # 2% of ₹2,500 bucket = ₹50 loss
    state = _eval(config, intraday_realised_pnl_today=Decimal("-50"))
    assert state.intraday_daily_loss == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.INTRADAY)
    assert not state.track_blocked(Track.SWING)


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


def test_swing_weekly_loss_at_4pct(config: RiskConfig) -> None:
    # 4% of ₹7,500 swing bucket = ₹300 loss
    state = _eval(config, swing_realised_pnl_this_week=Decimal("-300"))
    assert state.swing_weekly_loss == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.SWING)
    assert not state.track_blocked(Track.INTRADAY)


def test_swing_30d_loss_count(config: RiskConfig) -> None:
    state = _eval(config, swing_losing_trades_30d=4)
    assert state.swing_30d_loss_count == BreakerStatus.TRIPPED


def test_portfolio_daily_loss_blocks_all_tracks(config: RiskConfig) -> None:
    # 2% of ₹50,000 = ₹1,000 portfolio loss
    state = _eval(config, portfolio_realised_pnl_today=Decimal("-1000"))
    assert state.portfolio_daily_loss == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.LONG_TERM)
    assert state.track_blocked(Track.SWING)
    assert state.track_blocked(Track.INTRADAY)


def test_portfolio_max_drawdown_8pct(config: RiskConfig) -> None:
    state = _eval(config, live_drawdown_pct=Decimal("0.08"))
    assert state.portfolio_max_drawdown == BreakerStatus.TRIPPED
    assert state.requires_manual_resume()
    assert state.track_blocked(Track.LONG_TERM)


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


def test_long_term_unaffected_by_intraday_breakers(config: RiskConfig) -> None:
    # Intraday losses should not block long-term entries
    state = _eval(
        config,
        intraday_realised_pnl_today=Decimal("-50"),
        intraday_consecutive_losses_today=3,
    )
    assert not state.track_blocked(Track.LONG_TERM)
