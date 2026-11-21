"""Shared helpers for the Telegram command handlers (#103). Handlers read each
machine's own state table (trading_sessions, intraday_brackets, positions, bot_mode),
never the events table; every handler takes (db_path, args) and returns HTML text."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from alerts.format import esc, money  # noqa: F401  (re-exported for the handlers)

IST = ZoneInfo("Asia/Kolkata")


def conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def now_ist() -> datetime:
    return datetime.now(IST)


def today() -> str:
    return now_ist().date().isoformat()


def session_row(c: sqlite3.Connection, day: str | None = None) -> sqlite3.Row | None:
    return c.execute(
        "SELECT * FROM trading_sessions WHERE session_date=?", (day or today(),)
    ).fetchone()
