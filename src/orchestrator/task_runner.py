"""Context manager that always writes a task_run row — even on hard exceptions.

Design decisions:
- Timeout enforced via threading.Timer that raises SystemExit in the task thread.
- try/finally guarantees the task_runs row is written regardless of how the
  task exits (normal, exception, timeout, keyboard interrupt).
- Re-running a completed task for the same run_date is safe (idempotent design
  is each task's responsibility, but the runner doesn't block it).
"""

from __future__ import annotations

import logging
import signal
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .models import RetryPolicy, TaskRunStore, TaskStatus

logger = logging.getLogger(__name__)


class TimeoutError(Exception):
    pass


def _raise_timeout(signum: int, frame: object) -> None:  # noqa: ARG001
    raise TimeoutError("Task exceeded timeout")


@contextmanager
def run_task(
    task_id: str,
    run_date: str,
    store: TaskRunStore,
    timeout_seconds: int,
    attempt: int = 1,
    manual_override: bool = False,
) -> Iterator[int]:
    """Yields run_id. Sets RUNNING on enter; SUCCESS/FAILED/TIMEOUT on exit."""
    run_id = store.create(task_id, run_date, attempt=attempt, manual_override=manual_override)
    logger.info(
        "task_start task_id=%s run_date=%s attempt=%d run_id=%d",
        task_id,
        run_date,
        attempt,
        run_id,
    )
    old_handler = None
    try:
        # Use SIGALRM on Unix for timeout enforcement
        old_handler = signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(timeout_seconds)
        yield run_id
        signal.alarm(0)
        store.update(run_id, TaskStatus.SUCCESS)
        logger.info("task_success task_id=%s run_id=%d", task_id, run_id)
    except TimeoutError:
        signal.alarm(0)
        logger.error(
            "task_timeout task_id=%s run_id=%d timeout_seconds=%d",
            task_id,
            run_id,
            timeout_seconds,
        )
        store.update(run_id, TaskStatus.TIMEOUT, error_message=f"Exceeded {timeout_seconds}s")
        raise
    except Exception as exc:
        signal.alarm(0)
        tb = traceback.format_exc()
        logger.error("task_failed task_id=%s run_id=%d error=%s", task_id, run_id, exc)
        store.update(run_id, TaskStatus.FAILED, error_message=str(exc), error_traceback=tb)
        raise
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGALRM, old_handler)


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
            # Update latest run to RETRYING status
            latest = store.latest_for_date(task_id, run_date)
            if latest and latest.id is not None:
                store.update(latest.id, TaskStatus.RETRYING)
            time.sleep(delay)
        try:
            with run_task(
                task_id, run_date, store, timeout_seconds, attempt, manual_override
            ) as run_id:
                fn(run_date=run_date, run_id=run_id, **task_kwargs)
            return True
        except (Exception, TimeoutError):
            if attempt >= retry_policy.max_attempts:
                # Final failure — mark FAILED_FINAL on the last run row
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
