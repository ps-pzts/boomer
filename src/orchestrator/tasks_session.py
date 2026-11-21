"""Scheduled tasks: the every-minute Session machine tick (#63) and the daily Kite
checks that gate it (#85).

designs/state-machines.md §4.4. One task drives the whole day: it settles open
trades (stops, targets, OCO), moves the session through its states, halts on a
breaker or a stale feed, reconciles with the broker before resuming, squares
off at 15:14 and closes the day once the broker is flat.

08:55 `kite_preflight` and 09:15 `kite_canary` verify the real Kite plumbing
(paper days included) and record the result; the session tick reads it and will
not reach TRADING until both passed today. A failure alerts and halts the
session; only an operator re-run (Telegram /canary) clears it.
"""

from __future__ import annotations

import logging
import os
import time as _time
from collections.abc import Callable
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# NSE pre-open (09:00-09:15) rejects stop orders: the canary starts just after the open.
CANARY_NOT_BEFORE = dtime(9, 15, 5)


def _session_tick(run_date: str, run_id: int, session: object | None = None, **_: object) -> None:
    if session is None:
        logger.warning("session_tick: no session controller (execution not wired)")
        return
    state = session.tick()  # type: ignore[attr-defined]
    logger.info("session_tick run_date=%s state=%s", run_date, state)


def canary_config() -> object:
    """The canary's instrument and recording directory, from the environment."""
    from executor.kite_checks import CanaryConfig

    return CanaryConfig(
        symbol=os.environ.get("BOOMER_CANARY_SYMBOL", "INFY"),
        recordings_dir=os.environ.get("BOOMER_KITE_RECORDINGS_DIR", "data/kite_recordings"),
    )


def _default_alerter() -> object | None:
    try:
        from alerts.alerter import get_alerter

        return get_alerter()
    except Exception as exc:
        logger.error("kite_check_alerter_unavailable error=%s", exc)
        return None


def _kite_preflight(
    run_date: str,
    run_id: int,
    db_path: str,
    brokers: list | None = None,
    feed: object | None = None,
    alerter: object | None = None,
    **_: object,
) -> None:
    """08:55 IST: read-only Kite pre-flight. The result (pass or fail) is recorded;
    the task itself only fails if recording does."""
    from executor import kite_checks

    kite_checks.run_and_record(
        db_path,
        "preflight",
        kite_checks.kite_broker(brokers or []),
        feed=feed,
        alerter=alerter or _default_alerter(),
        source="scheduled",
        probe_symbol=os.environ.get("BOOMER_CANARY_SYMBOL", "INFY"),
    )


def _kite_canary(
    run_date: str,
    run_id: int,
    db_path: str,
    brokers: list | None = None,
    feed: object | None = None,
    alerter: object | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = _time.sleep,
    **_: object,
) -> None:
    """09:15 IST: the order canary on the real KiteBroker, never the paper broker."""
    from executor import kite_checks

    clock = clock or (lambda: datetime.now(IST))
    now = clock()
    start = now.replace(
        hour=CANARY_NOT_BEFORE.hour, minute=CANARY_NOT_BEFORE.minute, second=5, microsecond=0
    )
    if now < start:
        sleep((start - now).total_seconds())
    config = canary_config()
    if feed is not None:  # REST LTP falls back to the feed's tick for this symbol
        try:
            feed.subscribe([config.symbol])  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("canary_feed_subscribe_failed error=%s", exc)
    kite_checks.run_and_record(
        db_path,
        "canary",
        kite_checks.kite_broker(brokers or []),
        alerter=alerter or _default_alerter(),
        source="scheduled",
        canary_config=config,  # type: ignore[arg-type]
        clock=clock,
        sleep=sleep,
    )
