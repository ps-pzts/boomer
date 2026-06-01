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
    """Verify that recent EOD prices are present.

    Bhavcopy for today is only available after market close (~6 PM IST), so we
    check for the most recent trade_date in the DB and require it to be within
    the last 5 calendar days.  This correctly passes on Monday mornings when
    Friday's prices are the most recent available.
    """
    import sqlite3
    from datetime import date, timedelta

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT MAX(trade_date) as latest, COUNT(*) as n FROM prices").fetchone()
    conn.close()

    if row["n"] == 0 or row["latest"] is None:
        raise RuntimeError("No price data at all — EOD collector may have never run.")

    latest = date.fromisoformat(row["latest"])
    cutoff = date.fromisoformat(run_date) - timedelta(days=5)
    if latest < cutoff:
        raise RuntimeError(
            f"Most recent prices are from {latest} — more than 5 days old. "
            "EOD collector may have failed."
        )
    logger.info("data_check passed: latest price date=%s rows=%d", latest, row["n"])
