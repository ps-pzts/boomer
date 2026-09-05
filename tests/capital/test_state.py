"""Tests for CapitalStateManager and HWM mechanics."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from capital.models import Track
from capital.state import CapitalStateManager
from db.migrations import run_migrations

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    run_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture()
def mgr(db: Path) -> CapitalStateManager:
    return CapitalStateManager(db)


def test_initialise_creates_ledger_row(mgr: CapitalStateManager) -> None:
    row = mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    assert row.total_capital == Decimal("50000")
    assert row.high_water_mark == Decimal("50000")
    assert row.total_cash == Decimal("50000")
    assert row.consecutive_loss_days == 0
    assert row.eod_drawdown_pct == Decimal("0")


def test_initialise_is_idempotent(mgr: CapitalStateManager) -> None:
    mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    row2 = mgr.initialise(Decimal("99999"), date(2026, 1, 1))
    assert row2.total_capital == Decimal("50000")  # first value wins


def test_allocation_is_all_intraday_regardless_of_capital(mgr: CapitalStateManager) -> None:
    """Single track — allocation doesn't vary with capital level."""
    row_small = mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    assert row_small.intraday_allocated_pct == Decimal("1.00")

    row_large = mgr.write_eod_ledger(
        as_of=date(2026, 1, 2),
        total_capital=Decimal("500000"),
        total_cash=Decimal("500000"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("0"),
    )
    assert row_large.intraday_allocated_pct == Decimal("1.00")


def test_hwm_advances_on_gain(mgr: CapitalStateManager) -> None:
    mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    row = mgr.write_eod_ledger(
        as_of=date(2026, 1, 2),
        total_capital=Decimal("50600"),  # +600 gain
        total_cash=Decimal("50600"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("600"),
    )
    assert row.high_water_mark == Decimal("50600")
    assert row.peak_date == date(2026, 1, 2)
    assert row.consecutive_loss_days == 0


def test_hwm_does_not_decrease_on_loss(mgr: CapitalStateManager) -> None:
    mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    row = mgr.write_eod_ledger(
        as_of=date(2026, 1, 2),
        total_capital=Decimal("49985"),  # small loss
        total_cash=Decimal("49985"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("-15"),
    )
    assert row.high_water_mark == Decimal("50000")  # unchanged
    assert row.eod_drawdown_pct > Decimal("0")
    assert row.consecutive_loss_days == 1


def test_consecutive_loss_days_resets_on_gain(mgr: CapitalStateManager) -> None:
    mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    mgr.write_eod_ledger(
        as_of=date(2026, 1, 2),
        total_capital=Decimal("49900"),
        total_cash=Decimal("49900"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("-100"),
    )
    mgr.write_eod_ledger(
        as_of=date(2026, 1, 3),
        total_capital=Decimal("49800"),
        total_cash=Decimal("49800"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("-100"),
    )
    row = mgr.write_eod_ledger(
        as_of=date(2026, 1, 6),
        total_capital=Decimal("50200"),  # gain day
        total_cash=Decimal("50200"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("400"),
    )
    assert row.consecutive_loss_days == 0


def test_drawdown_calculation(mgr: CapitalStateManager) -> None:
    mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    # Capital drops to 46000 → drawdown = (50000 - 46000) / 50000 = 8%
    row = mgr.write_eod_ledger(
        as_of=date(2026, 1, 2),
        total_capital=Decimal("46000"),
        total_cash=Decimal("46000"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("-4000"),
    )
    expected_dd = Decimal("4000") / Decimal("50000")
    assert abs(row.eod_drawdown_pct - expected_dd) < Decimal("0.0001")


def test_bucket_available_calculation(mgr: CapitalStateManager) -> None:
    mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    row = mgr.write_eod_ledger(
        as_of=date(2026, 1, 2),
        total_capital=Decimal("50000"),
        total_cash=Decimal("43000"),
        intraday_deployed=Decimal("7000"),
        prev_eod_pnl_net=Decimal("0"),
    )
    # Single track: intraday bucket = 100% × 50000 = 50000; deployed 7000 → 43000 available
    assert row.bucket_capital(Track.INTRADAY) == Decimal("50000")
    assert row.bucket_available(Track.INTRADAY) == Decimal("43000")


# ------------------------------------------------------------------
# Worked example — single-track (intraday only)
# ------------------------------------------------------------------


def test_worked_example_single_track(mgr: CapitalStateManager) -> None:
    """Numerical example for the single-track (intraday-only) capital model.

    Day 0: start ₹50,000, allocation 100% intraday
    Day 1: intraday trade 1 share of stock at ₹500, stop ₹490
    Day 1 close: stop hit, loss ₹10 + costs ≈ ₹15
    Day 30: recovers with a ₹465 gain → HWM updates
    """
    # Day 0 initialisation
    row0 = mgr.initialise(Decimal("50000"), date(2026, 1, 1))
    assert row0.total_capital == Decimal("50000")
    assert row0.high_water_mark == Decimal("50000")
    assert row0.bucket_capital(Track.INTRADAY) == Decimal("50000")

    # Day 1: position sizing check
    # risk_per_trade = 0.5% × 50000 = ₹250
    # stop_distance = 500 - 490 = 10
    # shares = 250 / 10 = 25
    # position_value = 500 × 25 = ₹12,500
    # concentration = 12500 / 50000 = 25% — illustrative only, not gated here
    risk = Decimal("50000") * Decimal("0.005")  # ₹250
    stop_dist = Decimal("10")
    shares = int(risk / stop_dist)
    assert shares == 25

    # Day 1 close: loss
    row1 = mgr.write_eod_ledger(
        as_of=date(2026, 1, 2),
        total_capital=Decimal("49985"),
        total_cash=Decimal("49985"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("-15"),
    )
    assert row1.high_water_mark == Decimal("50000")
    assert row1.consecutive_loss_days == 1

    # Day 30: recovery
    row30 = mgr.write_eod_ledger(
        as_of=date(2026, 2, 1),
        total_capital=Decimal("50450"),
        total_cash=Decimal("50450"),
        intraday_deployed=Decimal("0"),
        prev_eod_pnl_net=Decimal("600"),
    )
    assert row30.high_water_mark == Decimal("50450")
    assert row30.peak_date == date(2026, 2, 1)
