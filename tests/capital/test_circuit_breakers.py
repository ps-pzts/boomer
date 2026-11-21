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


# --- boundaries: every limit trips exactly AT the limit and not one rupee / tick before ---
# Defaults on a Rs 50,000 base: daily 2% = Rs 1,000; weekly 4% = Rs 2,000; drawdown 8%;
# Nifty -3%; 3 consecutive losses; late entry from 14:30.


def test_intraday_daily_loss_boundary(config: RiskConfig) -> None:
    just_under = _eval(config, intraday_realised_pnl_today=Decimal("-999"))  # 1.998%
    assert just_under.intraday_daily_loss == BreakerStatus.CLEAR
    at_limit = _eval(config, intraday_realised_pnl_today=Decimal("-1000"))  # 2.000%
    assert at_limit.intraday_daily_loss == BreakerStatus.TRIPPED
    profit = _eval(config, intraday_realised_pnl_today=Decimal("5000"))
    assert profit.intraday_daily_loss == BreakerStatus.CLEAR


def test_intraday_daily_loss_uses_the_bucket_not_total_capital(config: RiskConfig) -> None:
    # bucket Rs 20,000: 2% = Rs 400, although total capital is Rs 50,000
    state = _eval(
        config,
        intraday_bucket_capital=Decimal("20000"),
        intraday_realised_pnl_today=Decimal("-400"),
    )
    assert state.intraday_daily_loss == BreakerStatus.TRIPPED
    state = _eval(
        config,
        intraday_bucket_capital=Decimal("20000"),
        intraday_realised_pnl_today=Decimal("-399"),
    )
    assert state.intraday_daily_loss == BreakerStatus.CLEAR


def test_portfolio_daily_loss_boundary(config: RiskConfig) -> None:
    assert (
        _eval(config, portfolio_realised_pnl_today=Decimal("-999")).portfolio_daily_loss
        == BreakerStatus.CLEAR
    )
    assert (
        _eval(config, portfolio_realised_pnl_today=Decimal("-1000")).portfolio_daily_loss
        == BreakerStatus.TRIPPED
    )


def test_portfolio_weekly_loss_boundary_and_blocks(config: RiskConfig) -> None:
    under = _eval(config, portfolio_realised_pnl_this_week=Decimal("-1999"))  # 3.998%
    assert under.portfolio_weekly_loss == BreakerStatus.CLEAR
    assert not under.track_blocked(Track.INTRADAY)
    at_limit = _eval(config, portfolio_realised_pnl_this_week=Decimal("-2000"))  # 4.000%
    assert at_limit.portfolio_weekly_loss == BreakerStatus.TRIPPED
    assert at_limit.track_blocked(Track.INTRADAY)
    assert not at_limit.requires_manual_resume()  # weekly loss clears by itself


def test_drawdown_boundary(config: RiskConfig) -> None:
    assert (
        _eval(config, live_drawdown_pct=Decimal("0.0799")).portfolio_max_drawdown
        == BreakerStatus.CLEAR
    )
    assert (
        _eval(config, live_drawdown_pct=Decimal("0.08")).portfolio_max_drawdown
        == BreakerStatus.TRIPPED
    )


def test_nifty_boundary_is_inclusive_and_one_sided(config: RiskConfig) -> None:
    assert _eval(config, nifty_intraday_move_pct=Decimal("-0.0299")).black_swan == (
        BreakerStatus.CLEAR
    )
    state = _eval(config, nifty_intraday_move_pct=Decimal("-0.03"))
    assert state.black_swan == BreakerStatus.TRIPPED
    assert state.track_blocked(Track.INTRADAY)
    # a +3% rally is not a black swan
    assert _eval(config, nifty_intraday_move_pct=Decimal("0.05")).black_swan == BreakerStatus.CLEAR


def test_consecutive_loss_boundary(config: RiskConfig) -> None:
    assert _eval(config, intraday_consecutive_losses_today=2).intraday_consecutive_losses == (
        BreakerStatus.CLEAR
    )
    assert _eval(config, intraday_consecutive_losses_today=3).intraday_consecutive_losses == (
        BreakerStatus.TRIPPED
    )


@pytest.mark.parametrize(
    "hour, minute, tripped",
    [(9, 59, False), (13, 45, False), (14, 0, False), (14, 29, False), (14, 30, True),
     (14, 59, True), (15, 0, True), (15, 20, True)],
)  # fmt: skip
def test_late_entry_clock(config: RiskConfig, hour: int, minute: int, tripped: bool) -> None:
    state = _eval(config, current_time_ist_hour=hour, current_time_ist_minute=minute)
    expected = BreakerStatus.TRIPPED if tripped else BreakerStatus.CLEAR
    assert state.intraday_late_entry == expected


def test_zero_capital_does_not_divide_by_zero(config: RiskConfig) -> None:
    state = _eval(
        config,
        intraday_bucket_capital=Decimal("0"),
        total_capital=Decimal("0"),
        intraday_realised_pnl_today=Decimal("-100"),
        portfolio_realised_pnl_today=Decimal("-100"),
        portfolio_realised_pnl_this_week=Decimal("-100"),
    )
    assert not state.any_tripped()


def test_loss_on_a_one_rupee_base_is_100pct(config: RiskConfig) -> None:
    state = _eval(
        config, intraday_bucket_capital=Decimal("1"), intraday_realised_pnl_today=Decimal("-1")
    )
    assert state.intraday_daily_loss == BreakerStatus.TRIPPED


@pytest.mark.parametrize(
    "field",
    [
        "intraday_daily_loss",
        "intraday_consecutive_losses",
        "intraday_late_entry",
        "portfolio_daily_loss",
        "portfolio_weekly_loss",
        "portfolio_max_drawdown",
        "black_swan",
    ],
)
def test_each_breaker_alone_blocks_entries(field: str) -> None:
    from capital.circuit_breakers import CircuitBreakerState

    clear = CircuitBreakerState.all_clear()
    assert not clear.any_tripped() and not clear.track_blocked(Track.INTRADAY)
    one = CircuitBreakerState(**{**clear.__dict__, field: BreakerStatus.TRIPPED})
    assert one.any_tripped()
    assert one.track_blocked(Track.INTRADAY)
    manual = field in ("portfolio_max_drawdown", "black_swan")
    assert one.requires_manual_resume() is manual


def test_state_is_immutable() -> None:
    from capital.circuit_breakers import CircuitBreakerState

    with pytest.raises(AttributeError):
        CircuitBreakerState.all_clear().black_swan = BreakerStatus.TRIPPED  # type: ignore[misc]
