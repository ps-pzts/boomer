"""Emergency-stop helpers for SessionController (#67/#65): cancel and flatten
whatever the bot does not track. Split out of session_controller.py (#95)."""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


class EmergencyMixin:
    """Mixed into SessionController; relies on its `_broker`, `_om`, `_sup`, `_conn`."""

    def _cancel_untracked_orders(self, day: date) -> None:
        """Emergency (#67/#65): cancel every working order the bot doesn't track.
        Our own orders were already cancelled by square_off_all."""
        stray = self.untracked_orders() or []
        for o in stray:
            try:
                self._broker.cancel_order(o["broker_order_id"])  # type: ignore[union-attr]
            except Exception as exc:
                logger.error("emergency_cancel_failed order=%s error=%s", o, exc)
        if stray:
            from observability.emit import emit

            emit(
                "decision",
                "emergency_cancel_untracked",
                severity="critical",
                entity_type="session",
                entity_id=day.isoformat(),
                orders=stray,
            )

    def _flatten_untracked(self, day: date) -> None:
        if self._om is None or self._broker is None:
            return
        from executor.emergency import flatten_untracked
        from observability.emit import emit

        tracked = set(self._sup.net_positions()) if self._sup is not None else set()  # type: ignore[attr-defined]
        actions = flatten_untracked(
            self._conn,
            self._om,
            self._broker.list_positions(),
            tracked,
            day,  # type: ignore[attr-defined]
        )
        if actions:
            emit(
                "decision",
                "emergency_flatten",
                severity="critical",
                entity_type="session",
                entity_id=day.isoformat(),
                actions=actions,
            )
            logger.error("emergency_flatten %s", actions)
