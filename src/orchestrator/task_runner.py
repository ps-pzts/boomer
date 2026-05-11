"""Task runner with thread-safe timeout enforcement.

Timeout is enforced via threading.Thread.join(timeout) — safe to call from any
thread, unlike signal.SIGALRM which only works in the main interpreter thread.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable

from .models import RetryPolicy, TaskRunStore, TaskStatus

logger = logging.getLogger(__name__)


def _call_with_timeout(
    fn: Callable[..., None], timeout_seconds: int, **kwargs: object
) -> tuple[Exception | None, str | None, bool]:
    """Run fn in a daemon thread. Returns (exc, traceback_str, timed_out)."""
    exc_holder: list[Exception | None] = [None]
    tb_holder: list[str | None] = [None]

    def _target() -> None:
        try:
            fn(**kwargs)
        except Exception as exc:
            exc_holder[0] = exc
            tb_holder[0] = traceback.format_exc()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)
    return exc_holder[0], tb_holder[0], t.is_alive()


def execute_with_retry(
    task_id: str,
    run_date: str,
    fn: Callable[..., None],
    store: TaskRunStore,
    retry_policy: RetryPolicy,
    timeout_seconds: int,
    manual_override: bool = False,
    task_kwargs: dict | None = None,
) -> bool:
    """Run fn up to retry_policy.max_attempts times. Return True on success."""
    task_kwargs = task_kwargs or {}
    for attempt in range(1, retry_policy.max_attempts + 1):
        delay = retry_policy.delay_for_attempt(attempt)
        if delay > 0:
            logger.info("task_retry_wait task_id=%s attempt=%d delay_s=%d", task_id, attempt, delay)
            latest = store.latest_for_date(task_id, run_date)
            if latest and latest.id is not None:
                store.update(latest.id, TaskStatus.RETRYING)
            time.sleep(delay)

        run_id = store.create(task_id, run_date, attempt=attempt, manual_override=manual_override)
        logger.info(
            "task_start task_id=%s run_date=%s attempt=%d run_id=%d",
            task_id,
            run_date,
            attempt,
            run_id,
        )

        exc, tb, timed_out = _call_with_timeout(
            fn, timeout_seconds, run_date=run_date, run_id=run_id, **task_kwargs
        )

        if timed_out:
            logger.error(
                "task_timeout task_id=%s run_id=%d timeout_seconds=%d",
                task_id,
                run_id,
                timeout_seconds,
            )
            store.update(run_id, TaskStatus.TIMEOUT, error_message=f"Exceeded {timeout_seconds}s")
        elif exc is not None:
            logger.error("task_failed task_id=%s run_id=%d error=%s", task_id, run_id, exc)
            store.update(run_id, TaskStatus.FAILED, error_message=str(exc), error_traceback=tb)
        else:
            store.update(run_id, TaskStatus.SUCCESS)
            logger.info("task_success task_id=%s run_id=%d", task_id, run_id)
            return True

        if attempt >= retry_policy.max_attempts:
            # Preserve TIMEOUT on final attempt; only escalate FAILED → FAILED_FINAL.
            if not timed_out:
                latest = store.latest_for_date(task_id, run_date)
                if latest and latest.id is not None:
                    store.update(
                        latest.id,
                        TaskStatus.FAILED_FINAL,
                        error_message=latest.error_message,
                        error_traceback=latest.error_traceback,
                    )
            logger.critical("task_failed_final task_id=%s run_date=%s", task_id, run_date)
            return False

    return False
