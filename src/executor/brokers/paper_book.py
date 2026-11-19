"""Persistence for the paper broker's book (#64).

The simulator keeps orders, MIS positions and cash in memory. A
restart used to lose them: the new process found open trades in our tables but
an empty paper broker, so the session halted with reconcile_mismatch and the
paper trades could no longer be managed. Now the whole book is written as one
JSON row per session day after every change, and restored when the paper broker
is built.

#86: the book is KiteSim's `to_state()` (versioned JSON); see
executor/brokers/kitesim/paper.py. The table is unchanged, so no migration.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class PaperBook:
    def __init__(self, db_path: str, session_date: date) -> None:
        self._db_path = db_path
        self._day = session_date.isoformat()

    def load(self) -> dict | None:
        with sqlite3.connect(self._db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT state FROM paper_broker_book WHERE session_date=?", (self._day,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state: dict) -> None:
        """Never raises: a failed save is logged; the in-memory book stays correct."""
        try:
            payload = json.dumps(state, default=str)
            with sqlite3.connect(self._db_path, timeout=10) as conn:
                conn.execute(
                    "INSERT INTO paper_broker_book (session_date, state, updated_at)"
                    " VALUES (?, ?, ?) ON CONFLICT(session_date) DO UPDATE SET"
                    " state=excluded.state, updated_at=excluded.updated_at",
                    (self._day, payload, datetime.now(IST).replace(tzinfo=None).isoformat()),
                )
        except Exception:
            logger.exception("paper_book_save_failed day=%s", self._day)
