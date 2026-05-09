"""
Collection run health tracking.

Persists one row per fetch attempt to `collection_runs`.
Drives the dashboard "data health" panel.
One source failing does not stop others — each run is isolated.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from collector.models import CollectionRunRow, DataSource, RunStatus


class CollectionRunStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def start(self, source: DataSource) -> CollectionRunRow:
        row = CollectionRunRow(
            run_id=str(uuid.uuid4()),
            source=source,
            started_at=_now_utc(),
        )
        self._db.execute(
            """
            INSERT INTO collection_runs
                (run_id, source, started_at, ended_at, status,
                 records_fetched, records_new, error_message, retry_count)
            VALUES (?, ?, ?, NULL, ?, 0, 0, NULL, 0)
            """,
            (row.run_id, row.source.value, _fmt_dt(row.started_at), RunStatus.RUNNING.value),
        )
        self._db.commit()
        return row

    def finish(self, row: CollectionRunRow) -> None:
        row.ended_at = _now_utc()
        self._db.execute(
            """
            UPDATE collection_runs
            SET ended_at=?, status=?, records_fetched=?, records_new=?,
                error_message=?, retry_count=?
            WHERE run_id=?
            """,
            (
                _fmt_dt(row.ended_at),
                row.status.value,
                row.records_fetched,
                row.records_new,
                row.error_message,
                row.retry_count,
                row.run_id,
            ),
        )
        self._db.commit()

    def latest(self, source: DataSource) -> CollectionRunRow | None:
        result = self._db.execute(
            """
            SELECT run_id, source, started_at, ended_at, status,
                   records_fetched, records_new, error_message, retry_count
            FROM collection_runs
            WHERE source = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (source.value,),
        ).fetchone()
        return _row_from_db(result) if result else None

    def recent_failures(self, source: DataSource, limit: int = 5) -> list[CollectionRunRow]:
        rows = self._db.execute(
            """
            SELECT run_id, source, started_at, ended_at, status,
                   records_fetched, records_new, error_message, retry_count
            FROM collection_runs
            WHERE source = ? AND status IN ('failed', 'partial')
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (source.value, limit),
        ).fetchall()
        return [_row_from_db(r) for r in rows]

    @contextmanager
    def run_context(self, source: DataSource):
        """
        Context manager that starts a run, yields the row for mutation,
        and finishes it on exit — setting status=failed if an exception escapes.

        Usage:
            with store.run_context(DataSource.BSE_FILINGS) as run:
                run.records_fetched = 42
                run.records_new = 10
                run.status = RunStatus.SUCCESS
        """
        row = self.start(source)
        try:
            yield row
        except Exception as exc:
            row.status = RunStatus.FAILED
            row.error_message = str(exc)[:1000]
            raise
        finally:
            self.finish(row)


# ── helpers ────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _row_from_db(row: tuple) -> CollectionRunRow:
    run_id, source, started_at, ended_at, status, fetched, new, err, retries = row
    return CollectionRunRow(
        run_id=run_id,
        source=DataSource(source),
        started_at=datetime.fromisoformat(started_at.rstrip("Z")),
        ended_at=datetime.fromisoformat(ended_at.rstrip("Z")) if ended_at else None,
        status=RunStatus(status),
        records_fetched=fetched,
        records_new=new,
        error_message=err,
        retry_count=retries,
    )
