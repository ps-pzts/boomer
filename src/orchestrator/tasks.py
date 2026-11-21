"""Scheduled task definitions.

Each task function signature: fn(run_date: str, run_id: int, **kwargs) -> None
Tasks call into the appropriate subsystem modules. External I/O (brokers, HTTP)
is performed by subsystem code — tasks are thin dispatch wrappers.

Implementations are split by responsibility:
  tasks_collector.py   — nightly EOD collection, data check
  tasks_brain.py       — batch features, intraday watchlist
  tasks_executor.py    — broker setup, intraday cycle, position review
  tasks_maintenance.py — EOD reconciliation, harvest, backup
"""

from __future__ import annotations

import logging

from .models import RetryPolicy, TaskDefinition
from .tasks_brain import (
    _intraday_watchlist,
    _morning_batch_features,
)
from .tasks_collector import (
    _early_morning_data_check,
    _nightly_eod_collector,
    _surveillance_refresh,
)
from .tasks_executor import (
    _feed_health_check,
    _intraday_cycle,
    _minute_bar_flush,
    _position_review,
    _pre_market_executor_setup,
)
from .tasks_maintenance import (
    _eod_reconciliation,
    _morning_heartbeat,
    _nightly_backup,
    _nightly_health_check,
    _weekly_harvest_check,
)
from .tasks_session import _kite_canary, _kite_preflight, _session_tick

logger = logging.getLogger(__name__)


def build_task_registry(
    db_path: str,
    archive_dir: str,
    backup_dir: str,
    reconciler: object | None = None,
    brokers: list | None = None,
    feed: object | None = None,
    bar_recorder: object | None = None,
    session: object | None = None,
    cycle: object | None = None,
) -> dict[str, TaskDefinition]:
    """Return all task definitions wired with runtime dependencies."""
    common = {"db_path": db_path, "archive_dir": archive_dir, "backup_dir": backup_dir}
    cycle_deps = {"cycle": cycle}
    broker_deps = {
        "brokers": brokers or [],
        "feed": feed,
        "bar_recorder": bar_recorder,
        "session": session,
    }
    session_deps = {"session": session}
    kite_check_deps = {"brokers": brokers or [], "feed": feed}
    bar_deps = {"bar_recorder": bar_recorder}
    feed_deps = {"feed": feed}
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
        "surveillance_refresh": TaskDefinition(
            task_id="surveillance_refresh",
            fn=_wrap(_surveillance_refresh, {}),  # type: ignore[arg-type]
            schedule="25,55 9-14 * * 1-5",  # 09:25..14:55 IST, just before each decision cycle
            dependencies=[],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),  # the next slot retries
            repeats=True,  # #61
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
        "intraday_watchlist": TaskDefinition(
            task_id="intraday_watchlist",
            fn=_wrap(_intraday_watchlist, feed_deps),  # type: ignore[arg-type]
            schedule="10 9 * * 1-5",  # 09:10 IST: subscribed before the 09:15 open
            dependencies=["morning_batch_features"],
            timeout_seconds=900,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=[60]),
        ),
        "pre_market_executor_setup": TaskDefinition(
            task_id="pre_market_executor_setup",
            fn=_wrap(_pre_market_executor_setup, broker_deps),  # type: ignore[arg-type]
            # 08:45 IST: token refresh + feed connect must finish before the 09:15
            # open (opening range = 09:15-09:29). Was 09:20, after recommendations,
            # only because the removed gtt_dispatch task followed it.
            schedule="45 8 * * 1-5",
            dependencies=[],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=[300]),
        ),
        "feed_health_check": TaskDefinition(
            task_id="feed_health_check",
            fn=_wrap(_feed_health_check, feed_deps),  # type: ignore[arg-type]
            schedule="17 9 * * 1-5",  # 09:17 IST: 2 min after open, ticks must be flowing
            dependencies=["pre_market_executor_setup"],
            timeout_seconds=60,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "minute_bar_flush": TaskDefinition(
            task_id="minute_bar_flush",
            fn=_wrap(_minute_bar_flush, bar_deps),  # type: ignore[arg-type]
            schedule="*/15 9-15 * * 1-5",  # 09:00-15:45 IST; 15:45 writes the last bars
            dependencies=[],
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=[30]),
            repeats=True,  # #61
        ),
        "intraday_cycle": TaskDefinition(
            task_id="intraday_cycle",
            fn=_wrap(_intraday_cycle, cycle_deps),  # type: ignore[arg-type]
            schedule="*/30 9-14 * * 1-5",  # 09:30–14:30 IST every 30 min
            dependencies=[],
            timeout_seconds=180,
            retry_policy=RetryPolicy(max_attempts=1),  # no retry — next cycle in 30 min
            repeats=True,  # #61
        ),
        "position_review": TaskDefinition(
            task_id="position_review",
            fn=_wrap(_position_review, {}),  # type: ignore[arg-type]
            schedule="0 9-15 * * 1-5",  # 09:00–15:00 IST hourly
            dependencies=[],
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=1),
            trailing_stop_task=True,  # runs even in paused mode
            repeats=True,  # #61
        ),
        "session_tick": TaskDefinition(
            task_id="session_tick",
            fn=_wrap(_session_tick, session_deps),  # type: ignore[arg-type]
            # Every minute 09:00-15:59 IST: manages open trades, moves the Session
            # machine, squares off from 15:14 (replaces intraday_squareoff, #63).
            schedule="* 9-15 * * 1-5",
            dependencies=[],
            timeout_seconds=55,
            retry_policy=RetryPolicy(max_attempts=1),  # next tick in a minute
            repeats=True,
            safety_task=True,  # runs in PAUSED and EMERGENCY_STOP (SM3)
        ),
        # #85: the session can't reach TRADING until both passed today. No retry: a
        # failure halts the session and needs an operator re-run (Telegram /canary).
        "kite_preflight": TaskDefinition(
            task_id="kite_preflight",
            fn=_wrap(_kite_preflight, kite_check_deps),  # type: ignore[arg-type]
            schedule="55 8 * * 1-5",  # 08:55 IST: after the 08:45 token refresh
            dependencies=[],
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "kite_canary": TaskDefinition(
            task_id="kite_canary",
            fn=_wrap(_kite_canary, kite_check_deps),  # type: ignore[arg-type]
            schedule="15 9 * * 1-5",  # starts 09:15:05: pre-open rejects stop orders
            dependencies=[],
            timeout_seconds=180,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "morning_heartbeat": TaskDefinition(
            task_id="morning_heartbeat",
            fn=_wrap(_morning_heartbeat, {}),  # type: ignore[arg-type]
            schedule="0 8 * * 1-5",  # 08:00 IST Mon–Fri
            dependencies=[],
            timeout_seconds=30,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=[60]),
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
        "nightly_health_check": TaskDefinition(
            task_id="nightly_health_check",
            fn=_wrap(_nightly_health_check, {}),  # type: ignore[arg-type]
            schedule="45 1 * * *",  # 01:45 IST — before 03:00 restart_guard
            dependencies=[],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
            run_on_holiday=True,
        ),
    }
