"""Main orchestrator supervisor process.

Entry point: `python -m src.orchestrator.orchestrator`

Runs as a long-lived process managed by systemd. On startup:
1. Runs database migrations
2. Marks any RUNNING task_runs as INTERRUPTED (recovery from crash)
3. Polls every 30 seconds for tasks to dispatch
4. Dispatches eligible tasks in dedicated threads with timeout + retry

Restart safety: systemd restarts within seconds if this process dies.
The 3 AM restart guard (ops/restart_guard.sh) checks task_runs before killing this process.
"""

from __future__ import annotations

import datetime
import logging
import os
import signal
import sys
import threading

from ..banner import log_boot_sequence, log_crash_recovery, log_shutdown
from .models import BotModeStore, TaskRunStore
from .scheduler import Scheduler
from .task_runner import execute_with_retry
from .tasks import build_task_registry

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        db_path: str,
        archive_dir: str,
        backup_dir: str,
        poll_interval: int = 30,
        intraday_runner: object | None = None,
        reconciler: object | None = None,
        brokers: list | None = None,
    ) -> None:
        self._db_path = db_path
        self._run_store = TaskRunStore(db_path)
        self._mode_store = BotModeStore(db_path)
        self._tasks = build_task_registry(
            db_path=db_path,
            archive_dir=archive_dir,
            backup_dir=backup_dir,
            intraday_runner=intraday_runner,
            reconciler=reconciler,
            brokers=brokers or [],
        )
        self._scheduler = Scheduler(
            task_registry=self._tasks,
            run_store=self._run_store,
            mode_store=self._mode_store,
            db_path=db_path,
            poll_interval_seconds=poll_interval,
        )
        self._poll_interval = poll_interval
        self._running_tasks: dict[str, threading.Thread] = {}
        self._last_dispatched: dict[str, datetime.datetime] = {}
        self._stop_event = threading.Event()

    # ─── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        interrupted = self._recover_interrupted_tasks()
        log_crash_recovery(interrupted)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self._loop()

    def _handle_signal(self, signum: int, frame: object) -> None:
        log_shutdown(signum)
        self._stop_event.set()

    def _recover_interrupted_tasks(self) -> int:
        """On startup, mark any RUNNING tasks from previous run as INTERRUPTED."""
        import datetime as dt
        import sqlite3

        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status='INTERRUPTED', ended_at=? WHERE status='RUNNING'",
                (now,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # ─── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now_utc = datetime.datetime.now(datetime.UTC)
            run_date = self._scheduler.current_run_date(now_utc)

            for task_id, task_def in self._tasks.items():
                if task_id in self._running_tasks and self._running_tasks[task_id].is_alive():
                    continue  # already running

                # Prevent double-firing within the same cron minute (poll runs every 30s).
                last = self._last_dispatched.get(task_id)
                if last and (now_utc - last).total_seconds() < 60:
                    continue

                should, reason = self._scheduler.should_run(task_def, now_utc, run_date)
                if not should:
                    continue

                logger.info("orchestrator_dispatch task_id=%s run_date=%s", task_id, run_date)
                self._last_dispatched[task_id] = now_utc
                t = threading.Thread(
                    target=self._run_task,
                    args=(task_def, run_date),
                    name=f"task-{task_id}",
                    daemon=True,
                )
                self._running_tasks[task_id] = t
                t.start()

            # Clean up finished threads
            self._running_tasks = {k: v for k, v in self._running_tasks.items() if v.is_alive()}

            self._stop_event.wait(timeout=self._poll_interval)

        logger.info("[FURY] Orchestrator loop exited")

    def _run_task(self, task_def: object, run_date: str) -> None:
        from .tasks import TaskDefinition  # local to avoid circular

        td: TaskDefinition = task_def  # type: ignore[assignment]
        success = execute_with_retry(
            task_id=td.task_id,
            run_date=run_date,
            fn=td.fn,
            store=self._run_store,
            retry_policy=td.retry_policy,
            timeout_seconds=td.timeout_seconds,
        )
        if td.task_id == "intraday_cycle":
            self._scheduler.record_intraday_result(run_date, success)
        if not success:
            self._emit_task_failure_alert(td.task_id, run_date)

    def _emit_task_failure_alert(self, task_id: str, run_date: str) -> None:
        try:
            from src.alerts.alerter import get_alerter

            alerter = get_alerter()
            alerter.critical(
                title=f"Task FAILED_FINAL: {task_id}",
                body=(
                    f"Task {task_id} exhausted all retries for run_date={run_date}."
                    " Needs intervention."
                ),
                source_task_id=task_id,
            )
        except Exception as exc:
            logger.error("alert_send_failed task_id=%s error=%s", task_id, exc)


def main() -> None:
    import os
    import pathlib

    from src.banner import print_banner
    from src.db.migrations import run_migrations

    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":%(message)s}',
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    print_banner()

    db_path = os.environ.get("BOOMER_DB_PATH", "/var/lib/boomer/boomer.db")
    archive_dir = os.environ.get("BOOMER_ARCHIVE_DIR", "/var/lib/boomer/archive")
    backup_dir = os.environ.get("BOOMER_BACKUP_DIR", "/var/lib/boomer/backups")
    poll_interval = int(os.environ.get("BOOMER_POLL_INTERVAL", "30"))

    migrations_dir = pathlib.Path(__file__).parents[3] / "migrations"
    run_migrations(db_path, migrations_dir)

    brokers = _build_brokers()

    orc = Orchestrator(
        db_path=db_path,
        archive_dir=archive_dir,
        backup_dir=backup_dir,
        poll_interval=poll_interval,
        brokers=brokers,
    )
    log_boot_sequence(db_path, poll_interval)
    orc.start()


def _build_brokers() -> list:
    """Instantiate and authenticate any brokers whose tokens are in the environment.

    Failures are logged as warnings — the orchestrator runs without broker
    connections (data pipeline still works, only EOD capital sync is skipped).
    """
    brokers = []

    kite_key = os.environ.get("KITE_API_KEY", "")
    kite_token = os.environ.get("KITE_ACCESS_TOKEN", "")
    if kite_key and kite_token:
        try:
            from src.executor.brokers.kite_broker import KiteBroker
            kite = KiteBroker()
            kite.authenticate()
            brokers.append(kite)
            logging.getLogger(__name__).info("broker_connected broker=kite")
        except Exception as exc:
            logging.getLogger(__name__).warning("broker_connect_failed broker=kite error=%s", exc)
    else:
        logging.getLogger(__name__).warning(
            "broker_not_configured broker=kite — set KITE_API_KEY + KITE_ACCESS_TOKEN in .env"
        )

    fyers_id = os.environ.get("FYERS_CLIENT_ID", "")
    fyers_token = os.environ.get("FYERS_ACCESS_TOKEN", "")
    if fyers_id and fyers_token:
        try:
            from src.executor.brokers.fyers_broker import FyersBroker
            fyers = FyersBroker()
            fyers.authenticate()
            brokers.append(fyers)
            logging.getLogger(__name__).info("broker_connected broker=fyers")
        except Exception as exc:
            logging.getLogger(__name__).warning("broker_connect_failed broker=fyers error=%s", exc)
    else:
        logging.getLogger(__name__).warning(
            "broker_not_configured broker=fyers — set FYERS_CLIENT_ID + FYERS_ACCESS_TOKEN in .env"
        )

    return brokers


if __name__ == "__main__":
    main()
