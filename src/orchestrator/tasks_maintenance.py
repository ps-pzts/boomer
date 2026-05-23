"""Scheduled task implementations: EOD reconciliation, harvest, backup, heartbeat."""

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


def _morning_heartbeat(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """8:00 AM IST heartbeat — proactive daily proof-of-life push to Telegram.

    If you don't receive this message on a trading day, the orchestrator is down.
    Also surfaces any circuit breakers tripped since yesterday.
    """
    import os
    import sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("morning_heartbeat: Telegram not configured — skipping")
        return

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        queued = conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE status='queued_for_execution'"
        ).fetchone()[0]
        awaiting = conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE status='awaiting_human'"
        ).fetchone()[0]
        tripped = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT breaker_name FROM circuit_breaker_events"
                " WHERE event_type='tripped' AND DATE(event_time)=?", (run_date,)
            ).fetchall()
        ]
    finally:
        conn.close()

    now_str = datetime.now(IST).strftime("%H:%M IST")
    cb_line = f"\n⚡ <b>Circuit breakers:</b> {', '.join(tripped)}" if tripped else ""
    approval_line = (
        f"\n⏳ <b>{awaiting} rec(s) awaiting your approval</b> — send /approve"
        if awaiting else ""
    )

    from alerts.telegram import send_telegram
    send_telegram(
        token, chat_id,
        f"✅ <b>Boomer — morning check-in</b> ({now_str})\n"
        f"Orchestrator is running. Today is {run_date}.\n"
        f"🚀 Queued for GTT at 09:25: {queued}"
        f"{approval_line}"
        f"{cb_line}"
    )
    logger.info(
        "morning_heartbeat sent run_date=%s queued=%d awaiting=%d",
        run_date, queued, awaiting,
    )


def _nightly_backup(run_date: str, run_id: int, db_path: str, backup_dir: str, **_: object) -> None:
    """Copy SQLite DB to daily backup directory."""
    import shutil

    backup_path = Path(backup_dir) / f"{run_date}.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup_path)
    logger.info("nightly_backup completed backup_path=%s", backup_path)
