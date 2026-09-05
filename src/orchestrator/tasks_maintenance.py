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

    from alerts.telegram import send_telegram
    send_telegram(
        token, chat_id,
        f"✅ <b>Boomer — morning check-in</b> ({now_str})\n"
        f"Orchestrator is running. Today is {run_date}.\n"
        f"🚀 Queued for GTT at 09:25: {queued}"
        f"{cb_line}"
    )
    logger.info(
        "morning_heartbeat sent run_date=%s queued=%d",
        run_date, queued,
    )


def _nightly_health_check(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """01:45 IST nightly self-healing check — runs before the 03:00 restart_guard.

    Checks:
    - SQLite PRAGMA integrity_check
    - WAL checkpoint (flush) + VACUUM (reclaim disk space)
    - Stuck RUNNING tasks (> 1 h old, likely orphaned after a crash)
    - Disk space (warn if < 20 % free)

    Sends a single Telegram report: ✅ all clear / ⚠️ issues found.
    """
    import os
    import shutil
    import sqlite3
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    issues: list[str] = []
    db_mb = 0.0
    free_pct = 100.0

    # 1. Integrity check + WAL checkpoint ─────────────────────────────────────
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                issues.append(f"DB integrity: {integrity}")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception as exc:
        issues.append(f"DB check failed: {exc}")

    # VACUUM requires no active WAL readers — use a fresh connection.
    try:
        vconn = sqlite3.connect(db_path, timeout=30)
        try:
            vconn.execute("VACUUM")
        finally:
            vconn.close()
        db_mb = os.path.getsize(db_path) / (1024 * 1024)
    except Exception as exc:
        issues.append(f"VACUUM failed: {exc}")

    # 2. Stuck RUNNING tasks (> 1 h) ──────────────────────────────────────────
    cutoff = (
        datetime.now(IST) - timedelta(hours=1)
    ).replace(tzinfo=None).isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            stuck = conn.execute(
                "SELECT task_id FROM task_runs"
                " WHERE status='RUNNING' AND started_at < ?",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        if stuck:
            names = ", ".join(r["task_id"] for r in stuck)
            issues.append(f"Stuck RUNNING task(s): {names}")
    except Exception as exc:
        issues.append(f"Stuck-task check failed: {exc}")

    # 3. Disk space ───────────────────────────────────────────────────────────
    try:
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)))
        free_pct = usage.free / usage.total * 100
        if free_pct < 20.0:
            free_gb = usage.free / (1024 ** 3)
            issues.append(f"Low disk space: {free_pct:.1f}% free ({free_gb:.1f} GB)")
    except Exception as exc:
        issues.append(f"Disk check failed: {exc}")

    # 4. Telegram report ──────────────────────────────────────────────────────
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info(
            "nightly_health_check run_date=%s issues=%d (Telegram not configured)",
            run_date, len(issues),
        )
        return

    from alerts.telegram import send_telegram

    if issues:
        lines = "\n".join(f"  ⚠️ {i}" for i in issues)
        msg = (
            f"⚠️ <b>Nightly health check — issues found</b> ({run_date})\n\n"
            f"{lines}\n\n"
            "<i>Review before market open.</i>"
        )
    else:
        msg = (
            f"✅ <b>Nightly health check — all clear</b> ({run_date})\n\n"
            f"  DB integrity: ok\n"
            f"  WAL checkpoint: done\n"
            f"  VACUUM: done — DB now {db_mb:.1f} MB\n"
            f"  Stuck tasks: none\n"
            f"  Disk free: {free_pct:.1f}%"
        )

    send_telegram(token, chat_id, msg)
    logger.info("nightly_health_check run_date=%s issues=%d", run_date, len(issues))


def _nightly_backup(run_date: str, run_id: int, db_path: str, backup_dir: str, **_: object) -> None:
    """Copy SQLite DB to daily backup directory."""
    import shutil

    backup_path = Path(backup_dir) / f"{run_date}.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup_path)
    logger.info("nightly_backup completed backup_path=%s", backup_path)
