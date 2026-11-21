"""Broker reconcile for SessionController (§4.5, #58, #65): our trades vs the
broker, and the report/halt it produces. Split out of session_controller.py (#95)."""

from __future__ import annotations

import json
import logging
from datetime import date

from executor.session_inputs import LegStatus, ReconcileReport, reconcile_report

logger = logging.getLogger(__name__)


class ReconcileMixin:
    """Mixed into SessionController; relies on its `_fire`, `_alert`, `_sup`, `_broker`,
    `_om` and `_broker_net`."""

    def _reconcile(self, day: date, ctx: dict) -> None:
        report = self.reconcile_report(day, ctx)
        from observability.emit import emit

        emit(
            "decision",
            "reconcile_report",
            severity="info" if report.clean else "critical",
            entity_type="session",
            entity_id=day.isoformat(),
            **report.as_inputs(),
        )
        if report.clean:
            self._fire(
                day,
                "reconciled",
                rule="reconcile_clean",
                inputs=report.as_inputs(),
                ctx=ctx,
                fields={"halt_reason": None, "halt_acked_at": None, "halt_detail": None},
            )
            return
        self._fire(
            day,
            "reconcile_failed",
            rule="reconcile_mismatch",
            inputs=report.as_inputs(),
            severity="critical",
            fields={
                "halt_reason": "reconcile_mismatch",
                "halt_acked_at": None,
                "halt_detail": json.dumps(
                    {"failed_checks": report.failures(), **report.checks}, default=str
                ),
            },
        )
        self._alert(
            "critical",
            "Reconcile MISMATCH — trading halted",
            f"Failed checks: {', '.join(report.failures())}. {report.checks}. "
            "The bot will not trade against this. Fix it at the broker, then acknowledge.",
        )

    def reconcile_report(self, day: date, ctx: dict) -> ReconcileReport:
        """§4.5: our trades vs the broker (source of truth), feed, watchlist."""
        legs = [LegStatus(*x) for x in self._sup.open_legs()] if self._sup else []  # type: ignore[attr-defined]
        return reconcile_report(
            trade_net=self._sup.net_positions() if self._sup else {},  # type: ignore[attr-defined]
            broker_net=self._broker_net(),
            open_legs=legs,
            feed_fresh=ctx["feed_fresh"],
            watchlist_size=ctx["watchlist_size"],
            untracked_orders=self.untracked_orders(),
        )

    def untracked_orders(self) -> list[dict] | None:
        """Orders working at the broker that we don't consider live (#65); None if
        the broker can't be asked."""
        if self._broker is None or self._om is None:
            return []
        try:
            ours = self._om.live_broker_order_ids()  # type: ignore[attr-defined]
            return [
                o
                for o in self._broker.list_open_orders()  # type: ignore[attr-defined]
                if o["broker_order_id"] not in ours
            ]
        except Exception:
            logger.exception("broker_open_orders_unavailable")
            return None
