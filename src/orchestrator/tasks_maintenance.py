"""Scheduled task implementations: EOD reconciliation, harvest, backup."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _eod_reconciliation(
    run_date: str,
    run_id: int,
    db_path: str,
    reconciler: object = None,
    brokers: list | None = None,
    **_: object,
) -> None:
    """Full EOD reconciliation: bot positions vs broker positions + capital sync."""
    brokers = brokers or []
    if reconciler is None and not brokers:
        logger.warning("eod_reconciliation: no reconciler or brokers configured, skipping")
        return
    if reconciler is not None:
        reconciler.run_eod(run_date=run_date)  # type: ignore[attr-defined]
    if brokers:
        from src.orchestrator.capital_sync import sync_eod_capital

        sync_eod_capital(db_path, brokers, run_date)
    logger.info("eod_reconciliation completed run_date=%s", run_date)


def _weekly_harvest_check(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Friday only: evaluate capital harvest threshold and persist if triggered."""
    import datetime as _dt

    from src.capital.harvest import SelfFundingHarvest
    from src.capital.state import CapitalStateManager

    capital_mgr = CapitalStateManager(db_path)
    ledger = capital_mgr.latest_ledger()
    if ledger is None:
        logger.warning("weekly_harvest_check: no capital ledger rows — skipping")
        return

    harvest_store = SelfFundingHarvest(db_path)
    harvest_date = _dt.date.fromisoformat(run_date)
    # Pass previous HWM (from ledger) and current total capital.
    result = harvest_store.run(
        current_total_capital=ledger.total_capital,
        previous_hwm=ledger.high_water_mark,
        harvest_date=harvest_date,
    )

    if result.fired:
        logger.info(
            "harvest_triggered amount=%.2f ops=%.2f dev=%.2f run_date=%s",
            result.harvest_amount,
            result.ops_credit,
            result.dev_credit,
            run_date,
        )
    else:
        logger.info("harvest_check: threshold not met run_date=%s", run_date)


def _nightly_backup(
    run_date: str, run_id: int, db_path: str, backup_dir: str, **_: object
) -> None:
    """Copy SQLite DB to daily backup directory."""
    import shutil

    backup_path = Path(backup_dir) / f"{run_date}.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup_path)
    logger.info("nightly_backup completed backup_path=%s", backup_path)
