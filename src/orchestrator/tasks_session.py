"""Scheduled task: the every-minute Session machine tick (#63).

designs/state-machines.md §4.4. One task drives the whole day: it settles open
trades (stops, targets, OCO), moves the session through its states, halts on a
breaker or a stale feed, reconciles with the broker before resuming, squares
off at 15:14 and closes the day once the broker is flat.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _session_tick(run_date: str, run_id: int, session: object | None = None, **_: object) -> None:
    if session is None:
        logger.warning("session_tick: no session controller (execution not wired)")
        return
    state = session.tick()  # type: ignore[attr-defined]
    logger.info("session_tick run_date=%s state=%s", run_date, state)
