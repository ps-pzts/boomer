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

    from src.collector.health import CollectionRunStore
    from src.collector.models import DataSource as _DataSource
    from src.collector.parser import build_fetcher_registry

    trade_date = _dt.date.fromisoformat(run_date)
    # Bulk deal files are published the next morning — always fetch the previous trading day.
    prev_trading_date = _prev_weekday(trade_date)
    _BULK_DEAL_SOURCES = {_DataSource.NSE_BULK_DEALS, _DataSource.BSE_BULK_DEALS}

    from src.collector.parser import ParseWorker

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
