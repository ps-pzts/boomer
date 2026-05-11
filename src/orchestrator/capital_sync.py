"""EOD capital ledger writer.

Fetches real cash balances from connected brokers, reads deployed position
values from the DB at cost basis, and writes a dated row to capital_ledger.
Called once per trading day after market close by the eod_reconciliation task.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from decimal import Decimal

logger = logging.getLogger(__name__)


def sync_eod_capital(db_path: str, brokers: list, run_date: str) -> None:
    """Fetch broker funds, compute deployed capital, write capital_ledger row.

    Args:
        db_path:  Path to the SQLite database.
        brokers:  List of authenticated Broker instances (Kite, Fyers, or both).
        run_date: ISO date string (YYYY-MM-DD) for this EOD run.
    """
    from capital.state import CapitalStateManager

    # ── 1. Fetch available cash from each broker ─────────────────────────────
    total_cash = Decimal("0")
    for broker in brokers:
        try:
            funds = broker.get_funds()
            cash = Decimal(str(funds.available_cash))
            total_cash += cash
            logger.info(
                "broker_funds broker=%s available_cash=%.2f used_margin=%.2f",
                broker.broker_id,
                funds.available_cash,
                funds.used_margin,
            )
        except Exception as exc:
            logger.error("get_funds_failed broker=%s error=%s", broker.broker_id, exc)

    # ── 2. Query deployed capital from positions table (cost basis) ───────────
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:

        def _deployed(track: str) -> Decimal:
            row = conn.execute(
                "SELECT COALESCE(SUM(quantity * average_entry_price), 0) AS v"
                " FROM positions WHERE is_open=1 AND track=?",
                (track,),
            ).fetchone()
            return Decimal(str(row["v"]))

        lt_deployed = _deployed("long_term")
        sw_deployed = _deployed("swing")
        id_deployed = _deployed("intraday")

        pnl_row = conn.execute(
            "SELECT COALESCE(SUM(realised_pnl), 0) AS pnl FROM positions WHERE DATE(exit_at)=?",
            (run_date,),
        ).fetchone()
        today_pnl = Decimal(str(pnl_row["pnl"]))
    finally:
        conn.close()

    # ── 3. Write capital ledger ───────────────────────────────────────────────
    total_capital = total_cash + lt_deployed + sw_deployed + id_deployed
    mgr = CapitalStateManager(db_path)
    as_of = date.fromisoformat(run_date)

    if mgr.latest_ledger() is None:
        # Seed a baseline row so write_eod_ledger has a "previous" to derive HWM from.
        mgr.initialise(total_capital, as_of)
        logger.info(
            "capital_initialised total_capital=%.2f run_date=%s",
            float(total_capital),
            run_date,
        )

    # Always write the full row with correct deployed amounts.
    # initialise() zeroes out deployed fields; this corrects that on first run too.
    mgr.write_eod_ledger(
        as_of=as_of,
        total_capital=total_capital,
        total_cash=total_cash,
        long_term_deployed=lt_deployed,
        swing_deployed=sw_deployed,
        intraday_deployed=id_deployed,
        prev_eod_pnl_net=today_pnl,
    )
    logger.info(
        "capital_eod_written total_capital=%.2f cash=%.2f run_date=%s",
        float(total_capital),
        float(total_cash),
        run_date,
    )
