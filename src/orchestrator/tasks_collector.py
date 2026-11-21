"""Scheduled task implementations: data collection."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _prev_weekday(d: object) -> object:
    """Return the most recent weekday before d (Mon→Fri, Tue-Fri→day-1, Sat→Fri, Sun→Fri)."""
    import datetime as _dt

    day: _dt.date = d  # type: ignore[assignment]
    days_back = {0: 3, 6: 2}.get(day.weekday(), 1)  # Mon=0→3, Sun=6→2, else 1
    return day - _dt.timedelta(days=days_back)


def _nightly_eod_collector(
    run_date: str, run_id: int, db_path: str, archive_dir: str, **_: object
) -> None:
    """Fetch EOD data from NSE/BSE: prices, filings, bulk deals, F&O OI."""
    import datetime as _dt
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path

    from collector.health import CollectionRunStore
    from collector.models import DataSource as _DataSource
    from collector.parser import build_fetcher_registry

    trade_date = _dt.date.fromisoformat(run_date)
    # Bulk deal files are published the next morning — always fetch the previous trading day.
    prev_trading_date = _prev_weekday(trade_date)
    _BULK_DEAL_SOURCES = {_DataSource.NSE_BULK_DEALS, _DataSource.BSE_BULK_DEALS}

    from collector.parser import ParseWorker

    db_conn = _sqlite3.connect(db_path, timeout=10)
    store = CollectionRunStore(db_conn)
    registry = build_fetcher_registry(db=db_conn, raw_dir=_Path(archive_dir))
    for source, fetcher in registry.items():
        fetch_date = prev_trading_date if source in _BULK_DEAL_SOURCES else trade_date
        with store.run_context(source):
            fetcher.run(trade_date=fetch_date)

    # Parse all newly archived raw rows into the domain tables.
    parse_stats = ParseWorker(db_conn, _Path(archive_dir), registry).run_pending(limit=5000)
    logger.info("nightly_eod_collector parse_stats=%s run_date=%s", parse_stats, run_date)
    db_conn.close()


def _early_morning_data_check(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Verify yesterday's prices are present."""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT COUNT(*) as n FROM prices WHERE trade_date=?", (run_date,)
    ).fetchone()
    conn.close()
    if row["n"] == 0:
        raise RuntimeError(f"No prices for trade_date={run_date}. EOD collector may have failed.")
    logger.info("data_check passed: %d price rows for %s", row["n"], run_date)


def _surveillance_refresh(
    run_date: str, run_id: int, db_path: str, archive_dir: str, **_: object
) -> None:
    """Retry the NSE ASM/GSM fetch while the market is open (#50 reconcile).

    The nightly collector may have found NSE unreachable. Every 30 minutes (just before
    each decision cycle) fetch whichever list is still missing for today; the decision
    cycle re-reads `surveillance_list` each time, so a list that finally arrives blocks
    NEW entries on listed stocks from the next cycle. Open trades are not touched.
    """
    import datetime as _dt
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path

    from collector.fetchers.surveillance import LIST_TYPES, SurveillanceFetcher

    conn = _sqlite3.connect(db_path, timeout=10)
    try:
        have = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT list_type FROM surveillance_list WHERE list_date = ?",
                (run_date,),
            )
        }
        missing = tuple(t for t in LIST_TYPES if t not in have)
        if not missing:
            return
        stored = SurveillanceFetcher(conn, _Path(archive_dir)).refresh(missing)
    finally:
        conn.close()
    if stored:
        logger.info("surveillance_refresh reconciled run_date=%s stored=%s", run_date, stored)
        try:
            from alerts.alerter import get_alerter

            get_alerter().info(
                "ASM/GSM list fetched mid-session",
                f"{stored} on {_dt.date.fromisoformat(run_date)}. Listed stocks are blocked "
                "from new entries; open trades are managed normally.",
                source_task_id="surveillance_refresh",
            )
        except Exception as exc:
            logger.error("alert_send_failed title=surveillance_refresh error=%s", exc)
    else:
        logger.warning(
            "surveillance_refresh still unavailable run_date=%s missing=%s", run_date, missing
        )
