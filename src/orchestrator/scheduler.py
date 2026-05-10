"""Cron-expression evaluator and dependency checker for the orchestrator.

Intentionally minimal — no external cron library needed for 12 tasks.
Uses croniter if available; falls back to a simple minute-resolution evaluator.
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from .models import BotMode, BotModeStore, TaskDefinition, TaskRunStore, TaskStatus, is_trading_day

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def cron_matches(expr: str, dt: datetime.datetime) -> bool:
    """Return True if `dt` (UTC) matches the cron expression (interpreted as UTC)."""
    try:
        from croniter import croniter  # type: ignore[import]

        cron = croniter(expr, dt - datetime.timedelta(minutes=1))
        return cron.get_next(datetime.datetime) <= dt
    except ImportError:
        pass
    # Minimal fallback: parse standard 5-field cron
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _field_matches(minute, dt.minute)
        and _field_matches(hour, dt.hour)
        and _field_matches(dom, dt.day)
        and _field_matches(month, dt.month)
        and _field_matches(dow, dt.weekday() + 1 if dt.weekday() < 6 else 0)  # Mon=1..Sun=0
    )


def _field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return value % step == 0
    if "-" in field:
        lo, hi = (int(x) for x in field.split("-"))
        return lo <= value <= hi
    # Comma-separated list
    if "," in field:
        return value in {int(x) for x in field.split(",")}
    return int(field) == value


def dependency_met(
    task: TaskDefinition,
    run_date: str,
    store: TaskRunStore,
    manual_override: bool = False,
) -> tuple[bool, str]:
    """Return (ok, reason). Checks each dependency succeeded today."""
    if manual_override:
        return True, "manual_override"
    for dep_id in task.dependencies:
        dep_run = store.latest_for_date(dep_id, run_date)
        if dep_run is None or dep_run.status != TaskStatus.SUCCESS:
            dep_status = dep_run.status if dep_run else "not_run"
            return False, f"dependency {dep_id} status={dep_status}"
    return True, "ok"


class Scheduler:
    """Polls every minute and dispatches tasks whose cron fires and dependencies are met."""

    def __init__(
        self,
        task_registry: dict[str, TaskDefinition],
        run_store: TaskRunStore,
        mode_store: BotModeStore,
        db_path: str,
        poll_interval_seconds: int = 30,
    ) -> None:
        self._tasks = task_registry
        self._run_store = run_store
        self._mode_store = mode_store
        self._db_path = db_path
        self._poll = poll_interval_seconds
        self._intraday_fail_count: dict[str, int] = {}

    def should_run(
        self, task: TaskDefinition, now_utc: datetime.datetime, run_date: str
    ) -> tuple[bool, str]:
        """Return (should_run, reason)."""
        mode = self._mode_store.current_mode()

        if mode == BotMode.EMERGENCY_STOP:
            return False, "emergency_stop"

        if mode == BotMode.PAUSED and not task.trailing_stop_task:
            return False, "paused"

        if not task.run_on_holiday and not is_trading_day(self._db_path, run_date):
            return False, "holiday"

        if not cron_matches(task.schedule, now_utc):
            return False, "cron_no_match"

        # Don't re-run a successful task for the same run_date
        existing = self._run_store.latest_for_date(task.task_id, run_date)
        if existing and existing.status == TaskStatus.SUCCESS:
            return False, "already_succeeded"

        dep_ok, dep_reason = dependency_met(task, run_date, self._run_store)
        if not dep_ok:
            return False, dep_reason

        # Intraday cycle: disabled for the rest of the day after 3 consecutive failures
        if task.task_id == "intraday_cycle":
            fails = self._intraday_fail_count.get(run_date, 0)
            if fails >= 3:
                return False, "intraday_disabled_after_3_failures"

        return True, "ok"

    def record_intraday_result(self, run_date: str, success: bool) -> None:
        if not success:
            self._intraday_fail_count[run_date] = self._intraday_fail_count.get(run_date, 0) + 1
        else:
            self._intraday_fail_count[run_date] = 0

    def current_run_date(self, now_utc: datetime.datetime) -> str:
        """Run date is the IST calendar date."""
        ist_now = now_utc.astimezone(IST)
        return ist_now.date().isoformat()
