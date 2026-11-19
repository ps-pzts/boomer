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
from zoneinfo import ZoneInfo

from ..banner import log_boot_sequence, log_crash_recovery, log_shutdown
from .models import BotModeStore, TaskRunStore
from .scheduler import Scheduler
from .task_runner import execute_with_retry
from .tasks import build_task_registry

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


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
        feed: object | None = None,
        bar_recorder: object | None = None,
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
            feed=feed,
            bar_recorder=bar_recorder,
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
        from zoneinfo import ZoneInfo

        now = (
            dt.datetime.now(ZoneInfo("Asia/Kolkata"))
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
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
            now_ist = datetime.datetime.now(_IST)
            run_date = self._scheduler.current_run_date(now_ist)

            for task_id, task_def in self._tasks.items():
                if task_id in self._running_tasks and self._running_tasks[task_id].is_alive():
                    continue  # already running

                # Prevent double-firing within the same cron minute (poll runs every 30s).
                last = self._last_dispatched.get(task_id)
                if last and (now_ist - last).total_seconds() < 60:
                    continue

                should, reason = self._scheduler.should_run(task_def, now_ist, run_date)
                if not should:
                    continue

                logger.info("orchestrator_dispatch task_id=%s run_date=%s", task_id, run_date)
                self._last_dispatched[task_id] = now_ist
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
    feed = _build_feed(brokers)
    bar_recorder = _build_bar_recorder(feed)
    _log_trading_mode(db_path)

    orc = Orchestrator(
        db_path=db_path,
        archive_dir=archive_dir,
        backup_dir=backup_dir,
        poll_interval=poll_interval,
        brokers=brokers,
        feed=feed,
        bar_recorder=bar_recorder,
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

    return brokers


def _build_feed(brokers: list) -> object | None:
    """One KiteFeed per process, sharing KiteBroker's instrument cache. Not connected
    here: at boot (03:00 restart) the token is yesterday's and already expired.
    pre_market_executor_setup connects it at 08:45 after the token refresh."""
    from src.executor.brokers.feeds.kite_feed import KiteFeed
    from src.executor.models import BrokerName

    kite = next((b for b in brokers if b.broker_id == BrokerName.KITE), None)
    if kite is None:
        logging.getLogger(__name__).warning("market_feed_not_configured — no Kite broker")
        return None
    feed = KiteFeed(instruments=kite.instruments)
    kite.attach_feed(feed)
    return feed


def _build_bar_recorder(feed: object | None) -> object | None:
    """Record every feed tick into 1-minute bars (design §3.2). Kite Personal has no
    historical candles, so this is the only intraday history the system gets."""
    if feed is None:
        return None
    from src.collector.live_bars import SessionBarRecorder

    root = os.environ.get("BOOMER_MINUTE_BARS_DIR", "/var/lib/boomer/parquet/minute_bars")
    recorder = SessionBarRecorder(root)
    feed.on_tick(recorder.on_tick)  # type: ignore[attr-defined]
    return recorder


def _log_trading_mode(db_path: str) -> None:
    """Resolve and log paper/live at boot. Execution broker wiring lands in B7 PR 4;
    until then nothing places orders, whatever the mode."""
    from src.executor.trading_mode import (
        ENV_VAR,
        LiveTradingAuthorisationStore,
        resolve_trading_mode,
    )

    decision = resolve_trading_mode(os.environ.get(ENV_VAR), LiveTradingAuthorisationStore(db_path))
    logging.getLogger(__name__).info(
        "boot_trading_mode mode=%s reason=%s", decision.mode, decision.reason
    )


if __name__ == "__main__":
    main()
