from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    FAILED_FINAL = "FAILED_FINAL"
    TIMEOUT = "TIMEOUT"
    INTERRUPTED = "INTERRUPTED"
    SKIPPED = "SKIPPED"


TERMINAL_STATUSES = frozenset({TaskStatus.SUCCESS, TaskStatus.FAILED_FINAL, TaskStatus.SKIPPED})


class BotMode(StrEnum):
    AUTO = "auto"
    PAUSED = "paused"
    EMERGENCY_STOP = "emergency_stop"


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_seconds: list[int] = field(default_factory=list)

    def delay_for_attempt(self, attempt: int) -> int:
        """Return wait seconds before `attempt` (1-indexed). 0 for first attempt."""
        if attempt <= 1:
            return 0
        idx = attempt - 2  # attempt 2 → index 0
        if idx < len(self.backoff_seconds):
            return self.backoff_seconds[idx]
        return self.backoff_seconds[-1] if self.backoff_seconds else 0


@dataclass
class TaskDefinition:
    task_id: str
    fn: Callable[..., None]
    schedule: str  # cron expression or event name
    dependencies: list[str]
    timeout_seconds: int
    retry_policy: RetryPolicy
    run_on_holiday: bool = False  # backup-type tasks run even on holidays
    trailing_stop_task: bool = False  # runs in paused mode (trailing stops)

    def description(self) -> str:
        return f"{self.task_id} [{self.schedule}]"


@dataclass
class TaskRun:
    id: int | None
    task_id: str
    run_date: str  # YYYY-MM-DD UTC
    status: TaskStatus
    started_at: str | None = None
    ended_at: str | None = None
    attempt: int = 1
    manual_override: bool = False
    error_message: str | None = None
    error_traceback: str | None = None


# ─── Bot mode ─────────────────────────────────────────────────────────────────


class BotModeStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def current_mode(self) -> BotMode:
        with self._conn() as conn:
            row = conn.execute("SELECT mode FROM bot_mode WHERE id = 1").fetchone()
        if row is None:
            return BotMode.AUTO
        return BotMode(row["mode"])

    def set_mode(
        self, new_mode: BotMode, changed_by: str = "system", reason: str | None = None
    ) -> None:
        import datetime
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)\
            .isoformat(timespec="seconds")
        with self._conn() as conn:
            old = conn.execute("SELECT mode FROM bot_mode WHERE id = 1").fetchone()
            old_mode = old["mode"] if old else BotMode.AUTO
            conn.execute(
                "UPDATE bot_mode SET mode=?, changed_at=?, changed_by=?, reason=? WHERE id=1",
                (new_mode, now, changed_by, reason),
            )
            conn.execute(
                "INSERT INTO bot_mode_log"
                " (old_mode, new_mode, changed_at, changed_by, reason) VALUES (?,?,?,?,?)",
                (old_mode, new_mode, now, changed_by, reason),
            )
            conn.commit()


# ─── Task run store ───────────────────────────────────────────────────────────


class TaskRunStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def create(
        self, task_id: str, run_date: str, attempt: int = 1, manual_override: bool = False
    ) -> int:
        import datetime
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)\
            .isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO task_runs
                   (task_id, run_date, status, started_at, attempt, manual_override)
                   VALUES (?,?,?,?,?,?)""",
                (task_id, run_date, TaskStatus.RUNNING, now, attempt, int(manual_override)),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def update(
        self,
        run_id: int,
        status: TaskStatus,
        error_message: str | None = None,
        error_traceback: str | None = None,
    ) -> None:
        import datetime
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)\
            .isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                """UPDATE task_runs SET status=?, ended_at=?, error_message=?, error_traceback=?
                   WHERE id=?""",
                (status, now, error_message, error_traceback, run_id),
            )
            conn.commit()

    def latest_for_date(self, task_id: str, run_date: str) -> TaskRun | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM task_runs
                   WHERE task_id=? AND run_date=?
                   ORDER BY id DESC LIMIT 1""",
                (task_id, run_date),
            ).fetchone()
        if row is None:
            return None
        return TaskRun(
            id=row["id"],
            task_id=row["task_id"],
            run_date=row["run_date"],
            status=TaskStatus(row["status"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            attempt=row["attempt"],
            manual_override=bool(row["manual_override"]),
            error_message=row["error_message"],
            error_traceback=row["error_traceback"],
        )

    def count_running(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM task_runs WHERE status='RUNNING'"
            ).fetchone()
        return row["n"] if row else 0

    def recent(self, hours: int = 24) -> list[TaskRun]:
        import datetime as _dt
        from zoneinfo import ZoneInfo

        now_ist = _dt.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
        cutoff = now_ist - _dt.timedelta(hours=hours)
        cutoff_str = cutoff.isoformat(timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_runs WHERE started_at >= ? ORDER BY started_at DESC",
                (cutoff_str,),
            ).fetchall()
        return [
            TaskRun(
                id=r["id"],
                task_id=r["task_id"],
                run_date=r["run_date"],
                status=TaskStatus(r["status"]),
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                attempt=r["attempt"],
                manual_override=bool(r["manual_override"]),
                error_message=r["error_message"],
                error_traceback=r["error_traceback"],
            )
            for r in rows
        ]


# ─── Trading calendar ─────────────────────────────────────────────────────────


def is_trading_day(db_path: str | Path, date_str: str) -> bool:
    """Return True if date_str (YYYY-MM-DD IST) is a trading day."""
    import datetime

    parsed = datetime.date.fromisoformat(date_str)
    if parsed.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT is_trading FROM trading_calendar WHERE trade_date=?", (date_str,)
        ).fetchone()
    finally:
        conn.close()
    if row is not None:
        return bool(row["is_trading"])
    return True  # default: weekdays are trading days unless listed as holiday
