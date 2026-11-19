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

# Operator emergency stop (#67): the dashboard sets the mode from another process,
# so it is polled. 5 s, not the 60 s session tick: "flat everything immediately".
EMERGENCY_POLL_SECONDS = 5
_EMERGENCY_WINDOW = (datetime.time(9, 0), datetime.time(16, 0))


class Orchestrator:
    def __init__(
        self,
        db_path: str,
        archive_dir: str,
        backup_dir: str,
        poll_interval: int = 30,
        reconciler: object | None = None,
        brokers: list | None = None,
        feed: object | None = None,
        bar_recorder: object | None = None,
        session: object | None = None,
        cycle: object | None = None,
    ) -> None:
        self._db_path = db_path
        self._session = session
        self._run_store = TaskRunStore(db_path)
        self._mode_store = BotModeStore(db_path)
        self._tasks = build_task_registry(
            db_path=db_path,
            archive_dir=archive_dir,
            backup_dir=backup_dir,
            reconciler=reconciler,
            brokers=brokers or [],
            feed=feed,
            bar_recorder=bar_recorder,
            session=session,
            cycle=cycle,
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
        threading.Thread(target=self._watch_emergency, name="emergency-watch", daemon=True).start()
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

    # ─── Emergency stop ────────────────────────────────────────────────────────

    def _watch_emergency(self) -> None:
        while not self._stop_event.wait(timeout=EMERGENCY_POLL_SECONDS):
            self.check_emergency(datetime.datetime.now(_IST))

    def check_emergency(self, now: datetime.datetime) -> bool:
        """Run the session step now if the operator hit EMERGENCY_STOP during market
        hours on a trading day. The session flattens everything and closes (#67);
        once closed, further calls return at once. Returns True if it ran."""
        from .models import BotMode, is_trading_day

        try:
            if self._session is None or not (
                _EMERGENCY_WINDOW[0] <= now.time() < _EMERGENCY_WINDOW[1]
            ):
                return False
            if self._mode_store.current_mode() != BotMode.EMERGENCY_STOP:
                return False
            if not is_trading_day(self._db_path, now.date().isoformat()):
                return False
            self._session.tick()  # type: ignore[attr-defined]
            return True
        except Exception as exc:  # the watcher must survive; the minute tick is the backstop
            logger.error("emergency_check_failed error=%s", exc)
            return False

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
    from src.orchestrator.bootstrap import bootstrap_capital, seed_risk_config

    today = datetime.datetime.now(_IST).date()
    seed_risk_config(db_path, today)  # #74: a fresh DB must be able to trade

    brokers = _build_brokers()
    bootstrap_capital(db_path, brokers, today)  # works after a mid-day restart; else 08:45
    feed = _build_feed(brokers)
    bar_recorder = _build_bar_recorder(feed)
    mode = _log_trading_mode(db_path)
    emitter = _start_event_stream(db_path, mode, feed)
    session = _build_session(db_path, None, feed, brokers)  # mode per session (#76)
    now = datetime.datetime.now(_IST)
    resume_market_session(db_path, feed, bar_recorder, now)
    if session is not None and _SESSION_RESUME_START <= now.time() < _SESSION_RESUME_END:
        session.on_restart(now.date())  # type: ignore[attr-defined]  # SM6: → RECONCILING

    orc = Orchestrator(
        db_path=db_path,
        archive_dir=archive_dir,
        backup_dir=backup_dir,
        poll_interval=poll_interval,
        brokers=brokers,
        feed=feed,
        bar_recorder=bar_recorder,
        session=session,
        cycle=_build_cycle(db_path, session, feed, bar_recorder),
    )
    log_boot_sequence(db_path, poll_interval)
    try:
        orc.start()  # returns when SIGTERM/SIGINT stops the loop
    finally:
        emitter.stop()  # `lifecycle/stopped` + flush queued events


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


# Window in which a restarted process must rejoin today's session: from the 08:45
# setup task to the 15:30 close. Outside it the scheduled tasks handle everything.
_SESSION_RESUME_START = datetime.time(8, 45)
_SESSION_RESUME_END = datetime.time(15, 30)


def resume_market_session(
    db_path: str, feed: object | None, bar_recorder: object | None, now: datetime.datetime
) -> bool:
    """Rejoin today's market session after a mid-session restart (#37).

    Tasks fire only on their cron minute, and a task that already succeeded today
    is never re-run, so after a restart at e.g. 11:00 neither the 08:45 setup nor
    the 09:10 watchlist runs again and the feed would stay disconnected all day.
    This replays their market-data side effects: start the bar session, subscribe
    NIFTY 50 plus today's persisted watchlist, connect the feed.

    Returns True if the session was resumed.
    """
    from src.brain.watchlist import WatchlistStore
    from src.executor.brokers.feeds.base import NIFTY_50
    from src.orchestrator.models import is_trading_day

    log = logging.getLogger(__name__)
    if feed is None:
        return False
    if not (_SESSION_RESUME_START <= now.time() < _SESSION_RESUME_END):
        return False
    today = now.date()
    if not is_trading_day(db_path, today.isoformat()):
        return False

    if bar_recorder is not None:
        bar_recorder.start_session(today)  # type: ignore[attr-defined]
    try:
        symbols = [e.stock_symbol for e in WatchlistStore(db_path).load(today)]
    except Exception as exc:  # table missing on a fresh DB, locked, ...
        log.warning("session_resume_watchlist_unavailable error=%s", exc)
        symbols = []
    try:
        feed.subscribe([NIFTY_50, *symbols])  # type: ignore[attr-defined]
        feed.reauthenticate()  # type: ignore[attr-defined]
    except Exception as exc:
        log.error("session_resume_feed_failed error=%s", exc)
        return False
    log.warning(
        "session_resumed_after_restart date=%s watchlist=%d (opening range is unavailable "
        "today for bars missed before the restart)",
        today,
        len(symbols),
    )
    return True


def _build_session(
    db_path: str, mode: str | None, feed: object | None, brokers: list
) -> object | None:
    """The Session machine + execution stack (#63). None only if wiring itself
    crashes; a missing feed or capital ledger still yields a session that can't trade."""
    from src.alerts.alerter import get_alerter
    from src.orchestrator.execution import build_session

    try:
        return build_session(db_path, mode, feed, brokers, alerter=get_alerter())
    except Exception as exc:
        logging.getLogger(__name__).exception("session_build_failed error=%s", exc)
        return None


def _build_cycle(
    db_path: str, session: object | None, feed: object | None, bar_recorder: object | None
) -> object | None:
    from src.orchestrator.execution import build_decision_cycle

    try:
        return build_decision_cycle(db_path, session, feed, bar_recorder)
    except Exception as exc:
        logging.getLogger(__name__).exception("decision_cycle_build_failed error=%s", exc)
        return None


def _log_trading_mode(db_path: str) -> str:
    """Resolve and log paper/live at boot; returns the mode the session executes in."""
    from src.executor.trading_mode import (
        ENV_VAR,
        LiveTradingAuthorisationStore,
        resolve_trading_mode,
    )

    decision = resolve_trading_mode(os.environ.get(ENV_VAR), LiveTradingAuthorisationStore(db_path))
    logging.getLogger(__name__).info(
        "boot_trading_mode mode=%s reason=%s", decision.mode, decision.reason
    )
    return decision.mode.value


def _start_event_stream(db_path: str, mode: str, feed: object | None) -> object:
    """One emitter for the orchestrator process (designs/observability.md §5, #56):
    `lifecycle/started` now, a `heartbeat` every 30 s carrying the feed's health."""
    from observability import emit as obs

    def health() -> dict:
        if feed is None:
            return {"feed": "not_configured"}
        h = feed.health()  # type: ignore[attr-defined]
        return {
            "feed_connected": h.connected,
            "feed_subscribed": h.subscribed,
            "feed_last_tick_at": h.last_tick_at,
        }

    return obs.configure(db_path, source="orchestrator", mode=mode, health=health)


if __name__ == "__main__":
    main()
