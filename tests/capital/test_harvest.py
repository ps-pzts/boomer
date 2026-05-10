"""Tests for SelfFundingHarvest and evaluate_harvest."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from capital.harvest import (
    SelfFundingHarvest,
    evaluate_harvest,
)
from capital.state import CapitalStateManager
from db.migrations import run_migrations

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"


# ------------------------------------------------------------------
# evaluate_harvest (pure function)
# ------------------------------------------------------------------


def test_no_harvest_when_below_hwm() -> None:
    result = evaluate_harvest(Decimal("49000"), Decimal("50000"))
    assert not result.fired
    assert result.harvest_amount == Decimal("0")


def test_no_harvest_when_excess_below_3pct() -> None:
    # HWM = 50000; excess must be >= 3% × 50000 = 1500 to fire; 51000 - 50000 = 1000 < 1500
    result = evaluate_harvest(Decimal("51000"), Decimal("50000"))
    assert not result.fired


def test_harvest_fires_at_threshold() -> None:
    # excess = 3% × 50000 = 1500 exactly
    result = evaluate_harvest(Decimal("51500"), Decimal("50000"))
    assert result.fired
    assert result.harvest_amount == Decimal("750")  # 50% × 1500
    assert result.ops_credit == Decimal("450")  # 60% × 750
    assert result.dev_credit == Decimal("300")  # 40% × 750


def test_harvest_split_sums_to_harvest_amount() -> None:
    result = evaluate_harvest(Decimal("55000"), Decimal("51800"))
    assert result.fired
    assert abs(result.ops_credit + result.dev_credit - result.harvest_amount) < Decimal("0.01")


def test_post_harvest_hwm_equals_post_capital() -> None:
    """After harvest, HWM is adjusted down by the withdrawal amount."""
    result = evaluate_harvest(Decimal("55000"), Decimal("51800"))
    assert result.post_harvest_hwm == result.post_harvest_capital


def test_worked_example_day90() -> None:
    """From Phase 1 design doc: Day 90 harvest.

    total_capital = 55,000
    previous HWM was 51,800
    excess = 3,200 = 6.2% of HWM > 3% trigger
    harvest_amount = 50% × 3,200 = 1,600
    ops_fund += 60% × 1,600 = 960
    dev_fund += 40% × 1,600 = 640
    new total_capital = 55,000 - 1,600 = 53,400
    new HWM = 53,400
    """
    result = evaluate_harvest(Decimal("55000"), Decimal("51800"))
    assert result.fired
    assert result.harvest_amount == Decimal("1600")
    assert result.ops_credit == Decimal("960")
    assert result.dev_credit == Decimal("640")
    assert result.post_harvest_capital == Decimal("53400")
    assert result.post_harvest_hwm == Decimal("53400")


# ------------------------------------------------------------------
# SelfFundingHarvest (persistence)
# ------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    run_migrations(db_path, MIGRATIONS_DIR)
    return db_path


def test_harvest_credits_funds(db: Path) -> None:
    """Harvest should credit ops and dev funds correctly.

    The harvest is called with previous_hwm=50000 and current_capital=55000
    (caller fetches previous_hwm BEFORE writing today's EOD row).
    """
    mgr = CapitalStateManager(db)
    mgr.initialise(Decimal("50000"), date(2026, 1, 1))

    harvest = SelfFundingHarvest(db)
    result = harvest.run(
        current_total_capital=Decimal("55000"),
        previous_hwm=Decimal("50000"),
        harvest_date=date(2026, 3, 28),
    )
    assert result.fired

    balances = harvest.fund_balances()
    assert abs(balances["ops"] - result.ops_credit) < Decimal("0.01")
    assert abs(balances["dev"] - result.dev_credit) < Decimal("0.01")
    assert balances["owner"] == Decimal("0")
    assert balances["tax"] == Decimal("0")


def test_harvest_does_not_fire_when_capital_equals_hwm(db: Path) -> None:
    """Harvest doesn't fire when total_capital == previous_hwm (no new peak)."""
    harvest = SelfFundingHarvest(db)
    result = harvest.run(
        current_total_capital=Decimal("53400"),
        previous_hwm=Decimal("53400"),  # post-harvest HWM from last week
        harvest_date=date(2026, 4, 4),
    )
    assert not result.fired


def test_ops_runway_months(db: Path) -> None:
    harvest = SelfFundingHarvest(db)
    # Manually seed ops balance
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE funds SET balance = 6000 WHERE fund_type = 'ops'")
    conn.commit()
    conn.close()

    runway = harvest.ops_runway_months(monthly_opex=Decimal("500"))
    assert runway == Decimal("12")
