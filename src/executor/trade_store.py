"""BracketStore: persistence for the Trade machine (#45, SM-2).

Split out of trade_lifecycle.py by responsibility: this module owns every read
and write of `intraday_brackets`, and every Trade-machine transition goes
through `advance()`, which commits the bracket fields, the state change and its
event in ONE transaction (designs/state-machines.md §3).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from executor.trade_machine import TRADE_SPEC
from statemachine import record_created, transition

ACTIVE_STATES = ("entry_pending", "protecting", "open", "exiting")


class BracketStore:
    def __init__(self, db: sqlite3.Connection, clock: Callable[[], datetime]) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row
        self._clock = clock

    def insert(
        self,
        bracket_id: str,
        req: Any,
        entry_order_id: str,
        now: datetime,
        entry_expires_at: datetime,
    ) -> None:
        """Persist a new trade in ENTRY_PENDING and record its birth, atomically."""
        self._db.execute(
            """INSERT INTO intraday_brackets (bracket_id, session_date, symbol, exchange,
                   planned_quantity, entry_price, stop_price, target_price, atr,
                   entry_order_id, entry_expires_at, state, trade_plan_id,
                   recommendation_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'entry_pending',?,?,?,?)""",
            (
                bracket_id,
                now.date().isoformat(),
                req.symbol,
                req.exchange,
                req.quantity,
                req.entry_price,
                req.stop_price,
                req.target_price,
                req.atr,
                entry_order_id,
                entry_expires_at.isoformat(),
                req.trade_plan_id,
                req.recommendation_id,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        record_created(
            self._db,
            TRADE_SPEC,
            bracket_id,
            rule="plan_valid",
            inputs={**vars(req), "entry_order_id": entry_order_id},
            correlation={"trade_id": bracket_id, "symbol": req.symbol},
        )
        self._db.commit()

    def advance(
        self,
        b: sqlite3.Row,
        trigger: str,
        *,
        rule: str,
        inputs: dict[str, Any] | None = None,
        severity: str = "info",
        **fields: Any,
    ) -> None:
        """Bracket field updates + state transition + event, in one transaction."""
        if fields:
            self._db.execute(
                f"UPDATE intraday_brackets SET {', '.join(f'{k}=?' for k in fields)}"
                " WHERE bracket_id=?",
                (*fields.values(), b["bracket_id"]),
            )  # transition() joins this open transaction
        transition(
            self._db,
            TRADE_SPEC,
            b["bracket_id"],
            trigger,
            rule=rule,
            inputs=inputs,
            severity=severity,
            correlation={"trade_id": b["bracket_id"], "symbol": b["symbol"]},
        )
        self._db.commit()

    def set(self, bracket_id: str, **fields: Any) -> None:
        """Non-state field update (e.g. leg order ids); the state is unchanged."""
        fields["updated_at"] = self._clock().replace(tzinfo=None).isoformat()
        self._db.execute(
            f"UPDATE intraday_brackets SET {', '.join(f'{k}=?' for k in fields)}"
            " WHERE bracket_id=?",
            (*fields.values(), bracket_id),
        )
        self._db.commit()

    def active(self) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(ACTIVE_STATES))
        q = f"SELECT * FROM intraday_brackets WHERE state IN ({marks}) ORDER BY created_at"
        return self._db.execute(q, ACTIVE_STATES).fetchall()

    def load(self, bracket_id: str) -> sqlite3.Row:
        return self._db.execute(
            "SELECT * FROM intraday_brackets WHERE bracket_id=?", (bracket_id,)
        ).fetchone()

    def exit_attempts(self, b: sqlite3.Row) -> int:
        """Exit orders ever created for this trade, failed ones included (#101): the
        next attempt needs a key no earlier attempt used."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM orders WHERE idempotency_key LIKE ?",
            (f"{b['bracket_id']}-exit%",),
        ).fetchone()
        return int(row[0])

    def existing_order_id(self, b: sqlite3.Row, leg: str) -> str | None:
        """A leg submitted before a crash, found by its idempotency key (restart safety)."""
        row = self._db.execute(
            "SELECT order_id FROM orders WHERE idempotency_key = ? ORDER BY created_at DESC",
            (f"{b['bracket_id']}-{leg}",),
        ).fetchone()
        return row[0] if row else None
