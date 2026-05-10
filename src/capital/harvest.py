from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class HarvestResult:
    fired: bool
    harvest_amount: Decimal
    ops_credit: Decimal
    dev_credit: Decimal
    post_harvest_capital: Decimal
    post_harvest_hwm: Decimal
    reason: str  # why it fired or didn't


# Harvest parameters (design-specified, recalibratable later — see Loophole 1 in Phase 1)
HARVEST_TRIGGER_PCT = Decimal("0.03")  # excess must be >= 3% of HWM
HARVEST_RATE = Decimal("0.50")  # take 50% of excess
OPS_SHARE = Decimal("0.60")  # of harvest: 60% → ops_fund
DEV_SHARE = Decimal("0.40")  # of harvest: 40% → dev_fund


def evaluate_harvest(
    current_total_capital: Decimal,
    previous_hwm: Decimal,
) -> HarvestResult:
    """Evaluate whether the weekly harvest should fire. Pure function — no DB access.

    Called every Friday after EOD reconciliation, BEFORE writing the Friday EOD ledger row.
    Caller must pass the HWM from the PREVIOUS state (Thursday's or last-Friday's ledger row),
    NOT today's HWM — today's total_capital may already exceed that HWM, which is the point.

    Order of operations (Loophole 7 in Phase 1):
      1. Is current_total_capital > previous_hwm? (new peak this week)
      2. Is excess >= 3% of previous_hwm?
      3. Compute harvest_amount = 50% × excess.
      4. post_hwm = current_total_capital - harvest_amount (capital-withdrawal adjustment).
    """
    capital = current_total_capital
    hwm = previous_hwm

    if capital <= hwm:
        return HarvestResult(
            fired=False,
            harvest_amount=Decimal("0"),
            ops_credit=Decimal("0"),
            dev_credit=Decimal("0"),
            post_harvest_capital=capital,
            post_harvest_hwm=hwm,
            reason="current_total_capital <= previous_hwm — no new peak this week",
        )

    excess = capital - hwm
    trigger_threshold = hwm * HARVEST_TRIGGER_PCT

    if excess < trigger_threshold:
        return HarvestResult(
            fired=False,
            harvest_amount=Decimal("0"),
            ops_credit=Decimal("0"),
            dev_credit=Decimal("0"),
            post_harvest_capital=capital,
            post_harvest_hwm=capital,  # HWM advances even when harvest doesn't fire
            reason=f"excess={excess:.2f} < threshold={trigger_threshold:.2f} (3% of HWM)",
        )

    harvest_amount = excess * HARVEST_RATE
    ops_credit = harvest_amount * OPS_SHARE
    dev_credit = harvest_amount * DEV_SHARE
    post_capital = capital - harvest_amount
    post_hwm = capital - harvest_amount  # HWM adjusts down by the withdrawal amount

    return HarvestResult(
        fired=True,
        harvest_amount=harvest_amount,
        ops_credit=ops_credit,
        dev_credit=dev_credit,
        post_harvest_capital=post_capital,
        post_harvest_hwm=post_hwm,
        reason=f"excess={excess:.2f} >= threshold={trigger_threshold:.2f}; harvesting 50%",
    )


class SelfFundingHarvest:
    """Persists harvest events and credits to funds. Called by the orchestrator weekly task."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def run(
        self,
        current_total_capital: Decimal,
        previous_hwm: Decimal,
        harvest_date: date,
    ) -> HarvestResult:
        """Evaluate and persist harvest.

        Must be called BEFORE writing the Friday EOD ledger row so the caller
        can use post_harvest_hwm as the HWM for that row.
        """
        result = evaluate_harvest(current_total_capital, previous_hwm)
        if not result.fired:
            return result

        event_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO harvest_events (
                    event_id, harvest_date,
                    pre_harvest_capital, pre_harvest_hwm,
                    excess, harvest_amount,
                    ops_credit, dev_credit,
                    post_harvest_capital, post_harvest_hwm,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    harvest_date.isoformat(),
                    float(current_total_capital),
                    float(previous_hwm),
                    float(current_total_capital - previous_hwm),
                    float(result.harvest_amount),
                    float(result.ops_credit),
                    float(result.dev_credit),
                    float(result.post_harvest_capital),
                    float(result.post_harvest_hwm),
                    now,
                ),
            )
            conn.execute(
                "UPDATE funds SET balance = balance + ?, last_updated = ? WHERE fund_type = 'ops'",
                (float(result.ops_credit), now),
            )
            conn.execute(
                "UPDATE funds SET balance = balance + ?, last_updated = ? WHERE fund_type = 'dev'",
                (float(result.dev_credit), now),
            )

        return result

    def fund_balances(self) -> dict[str, Decimal]:
        with self._conn() as conn:
            rows = conn.execute("SELECT fund_type, balance FROM funds").fetchall()
        return {row["fund_type"]: Decimal(str(row["balance"])) for row in rows}

    def ops_runway_months(self, monthly_opex: Decimal) -> Decimal:
        """How many months of runway does the ops fund have?"""
        if monthly_opex <= 0:
            return Decimal("0")
        balances = self.fund_balances()
        return balances.get("ops", Decimal("0")) / monthly_opex
