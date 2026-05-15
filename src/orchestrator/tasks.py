"""12 scheduled task definitions.

Each task function signature: fn(run_date: str, run_id: int, **kwargs) -> None
Tasks call into the appropriate subsystem modules. External I/O (brokers, HTTP)
is performed by subsystem code — tasks are thin dispatch wrappers.

Implementations are split by responsibility:
  tasks_collector.py   — nightly EOD collection, data check
  tasks_brain.py       — features, signals, recommendations
  tasks_executor.py    — broker setup, intraday cycle, position review
  tasks_maintenance.py — EOD reconciliation, harvest, backup
"""

from __future__ import annotations

import logging

from .models import RetryPolicy, TaskDefinition
from .tasks_brain import (
    _morning_batch_features,
    _morning_batch_recommendations,
    _morning_batch_signals,
)
from .tasks_collector import _early_morning_data_check, _nightly_eod_collector
from .tasks_executor import (
    _intraday_cycle,
    _intraday_squareoff,
    _position_review,
    _pre_market_executor_setup,
)
from .tasks_maintenance import _eod_reconciliation, _nightly_backup, _weekly_harvest_check

logger = logging.getLogger(__name__)


def build_task_registry(
    db_path: str,
    archive_dir: str,
    backup_dir: str,
    intraday_runner: object | None = None,
    reconciler: object | None = None,
    brokers: list | None = None,
) -> dict[str, TaskDefinition]:
    """Return all 12 task definitions wired with runtime dependencies."""
    common = {"db_path": db_path, "archive_dir": archive_dir, "backup_dir": backup_dir}
    intraday_deps = {"intraday_runner": intraday_runner}
    broker_deps = {"brokers": brokers or []}
    eod_deps = {"reconciler": reconciler, "brokers": brokers or []}

    def _wrap(fn: object, extra: dict) -> object:
        import functools

        @functools.wraps(fn)  # type: ignore[arg-type]
        def wrapped(**kwargs: object) -> None:
            fn(**{**common, **extra, **kwargs})  # type: ignore[call-arg]

        return wrapped

    return {
        "nightly_eod_collector": TaskDefinition(
            task_id="nightly_eod_collector",
            fn=_wrap(_nightly_eod_collector, {}),  # type: ignore[arg-type]
            schedule="30 7 * * *",  # 07:30 IST
            dependencies=[],
            timeout_seconds=1800,
            retry_policy=RetryPolicy(max_attempts=4, backoff_seconds=[300, 900, 2700]),
            run_on_holiday=True,
        ),
        "early_morning_data_check": TaskDefinition(
            task_id="early_morning_data_check",
            fn=_wrap(_early_morning_data_check, {}),  # type: ignore[arg-type]
            schedule="0 9 * * 1-5",  # 09:00 IST
            dependencies=["nightly_eod_collector"],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "morning_batch_features": TaskDefinition(
            task_id="morning_batch_features",
            fn=_wrap(_morning_batch_features, {}),  # type: ignore[arg-type]
            schedule="5 9 * * 1-5",  # 09:05 IST
            dependencies=["early_morning_data_check"],
            timeout_seconds=600,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "morning_batch_signals": TaskDefinition(
            task_id="morning_batch_signals",
            fn=_wrap(_morning_batch_signals, {}),  # type: ignore[arg-type]
            schedule="10 9 * * 1-5",  # 09:10 IST
            dependencies=["morning_batch_features"],
            timeout_seconds=900,
            retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=[600, 600]),
        ),
        "morning_batch_recommendations": TaskDefinition(
            task_id="morning_batch_recommendations",
            fn=_wrap(_morning_batch_recommendations, {}),  # type: ignore[arg-type]
            schedule="15 9 * * 1-5",  # 09:15 IST
            dependencies=["morning_batch_signals"],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "pre_market_executor_setup": TaskDefinition(
            task_id="pre_market_executor_setup",
            fn=_wrap(_pre_market_executor_setup, broker_deps),  # type: ignore[arg-type]
            schedule="20 9 * * 1-5",  # 09:20 IST
            dependencies=["morning_batch_recommendations"],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "intraday_cycle": TaskDefinition(
            task_id="intraday_cycle",
            fn=_wrap(_intraday_cycle, intraday_deps),  # type: ignore[arg-type]
            schedule="*/30 9-14 * * 1-5",  # 09:30–14:30 IST every 30 min
            dependencies=[],
            timeout_seconds=180,
            retry_policy=RetryPolicy(max_attempts=1),  # no retry — next cycle in 30 min
        ),
        "position_review": TaskDefinition(
            task_id="position_review",
            fn=_wrap(_position_review, {}),  # type: ignore[arg-type]
            schedule="0 9-15 * * 1-5",  # 09:00–15:00 IST hourly
            dependencies=[],
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=1),
            trailing_stop_task=True,  # runs even in paused mode
        ),
        "intraday_squareoff": TaskDefinition(
            task_id="intraday_squareoff",
            fn=_wrap(_intraday_squareoff, intraday_deps),  # type: ignore[arg-type]
            schedule="14 15 * * 1-5",  # 15:14 IST
            dependencies=[],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "eod_reconciliation": TaskDefinition(
            task_id="eod_reconciliation",
            fn=_wrap(_eod_reconciliation, eod_deps),  # type: ignore[arg-type]
            schedule="0 16 * * 1-5",  # 16:00 IST
            dependencies=[],
            timeout_seconds=600,
            retry_policy=RetryPolicy(max_attempts=6, backoff_seconds=[600, 600, 600, 600, 600]),
        ),
        "weekly_harvest_check": TaskDefinition(
            task_id="weekly_harvest_check",
            fn=_wrap(_weekly_harvest_check, {}),  # type: ignore[arg-type]
            schedule="30 16 * * 5",  # 16:30 IST Friday
            dependencies=["eod_reconciliation"],
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "nightly_backup": TaskDefinition(
            task_id="nightly_backup",
            fn=_wrap(_nightly_backup, {}),  # type: ignore[arg-type]
            schedule="0 23 * * *",  # 23:00 IST
            dependencies=[],
            timeout_seconds=900,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=[300]),
            run_on_holiday=True,
        ),
    }
