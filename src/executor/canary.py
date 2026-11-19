"""09:15 Kite order canary (#85): prove order placement works before trading.

NSE pre-open (09:00-09:15) rejects stop orders, so this runs at 09:15:05. On a
liquid instrument it places two MIS orders that cannot fill in normal markets:

  (a) SL-M SELL qty 1, trigger = LTP x (1 - offset) rounded UP to the tick
  (b) LIMIT BUY qty 1, price   = LTP x (1 - offset) rounded DOWN to the tick

then modifies each a few ticks further away, cancels each, and verifies through
the broker's own status mapping that both ended CANCELLED with nothing filled
and no canary-tagged order is still working.

Safety:
  * Drift guard: if the LTP comes within `guard_pct` of any canary price, both
    are cancelled at once (a triggered SL-M SELL with no position opens a short).
  * Any exception, unexpected status, fill or leftover = failure, and every
    working canary-tagged order (this run's or an earlier one's) is cancelled.
  * A filled canary order is NOT flattened here: the session halts
    (`canary_failed`) and its broker reconcile reports the stray 1-share
    position for the operator.
  * Canary orders are not trades: only the broker is used, never OrderManager.
    The canary always runs on the real KiteBroker, paper days included.
  * Moving the canary SELL stop further away is not a widened trade stop
    (CLAUDE.md §10): it protects no position.

Every raw Kite response (ltp, place, modify, cancel, orders) is written to
`<recordings_dir>/YYYY-MM-DD/canary-HHMMSS.json` for use as test fixtures.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, ProductType
from executor.preflight import CheckReport, CheckResult
from executor.ticks import round_to_tick

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
TAG_PREFIX = "CNRY"
_DONE = frozenset({"COMPLETE", "CANCELLED", "CANCELLED AMO", "REJECTED"})
_BAD = {
    OrderStatus.REJECTED: "rejected",
    OrderStatus.FILLED: "filled",
    OrderStatus.PARTIAL: "filled",
    OrderStatus.TRIGGERED: "triggered",
}


@dataclass(frozen=True)
class CanaryConfig:
    symbol: str = "INFY"
    exchange: str = "NSE"
    offset_pct: float = 0.03  # inside the circuit band of a large cap
    guard_pct: float = 0.01  # LTP this close to a canary price -> cancel now
    modify_ticks: int = 5
    max_polls: int = 20
    poll_interval_s: float = 0.5
    recordings_dir: str = "data/kite_recordings"


class CanaryAbort(Exception):
    pass


def run_canary(
    broker: object,
    config: CanaryConfig | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(IST),
    sleep: Callable[[float], None] = _time.sleep,
) -> CheckReport:
    """Run the canary against `broker` (a KiteBroker). Never raises."""
    return _Canary(broker, config or CanaryConfig(), clock, sleep).run()


class _Canary:
    def __init__(self, broker, cfg: CanaryConfig, clock, sleep) -> None:
        self.b, self.cfg, self.clock, self.sleep = broker, cfg, clock, sleep
        self.key = f"{cfg.exchange}:{cfg.symbol}"
        self.results: list[CheckResult] = []
        self.responses: list[dict] = []
        self.ids: dict[str, str] = {}  # leg -> broker order id
        self.prices: dict[str, float] = {}  # leg -> trigger / limit price
        self.started = clock()

    # ── Flow ──────────────────────────────────────────────────────────────────

    def run(self) -> CheckReport:
        try:
            self.b.set_response_recorder(self._record)
        except Exception:
            logger.exception("canary_recorder_unavailable")
        try:
            self._steps()
        except Exception as exc:
            detail = str(exc) if isinstance(exc, CanaryAbort) else f"{type(exc).__name__}: {exc}"
            self.results.append(CheckResult("aborted", False, detail))
            self.results.append(self._cleanup())
        finally:
            with contextlib.suppress(Exception):
                self.b.set_response_recorder(None)
        report = CheckReport(
            "canary",
            tuple(self.results),
            self.started.replace(tzinfo=None).isoformat(),
            extra={"symbol": self.key, "order_ids": dict(self.ids)},
        )
        path = self._write_recording(report)
        if path:
            report.extra["recording"] = path
        return report

    def _steps(self) -> None:
        ltp = self._ltp()
        tick = self._tick()
        base = ltp * (1 - self.cfg.offset_pct)
        self.prices["sl"] = round_to_tick(base, tick, up=True)
        self.prices["limit"] = round_to_tick(base, tick, up=False)
        self._ok("ltp", f"{self.key} ltp={ltp} tick={tick}")

        self._place("sl", OrderSide.SELL, OrderType.SL, trigger=self.prices["sl"])
        self._place("limit", OrderSide.BUY, OrderType.LIMIT, price=self.prices["limit"])
        for leg in self.ids:
            self._await(leg, OrderStatus.PENDING)
        self._ok("resting", f"sl_trigger={self.prices['sl']} limit={self.prices['limit']}")

        self._guard()
        away = self.cfg.modify_ticks * tick
        new = {
            "sl": round_to_tick(self.prices["sl"] - away, tick, up=True),
            "limit": round_to_tick(self.prices["limit"] - away, tick, up=False),
        }
        if min(new.values()) <= 0:
            raise CanaryAbort(f"modify would price at or below zero: {new}")
        self.b.modify_order(self.ids["sl"], {"trigger_price": new["sl"]})
        self.b.modify_order(self.ids["limit"], {"price": new["limit"]})
        self.prices.update(new)
        self._await_modified()
        self._ok("modify", f"sl_trigger={new['sl']} limit={new['limit']}")

        self._guard()
        for leg in self.ids:
            self.b.cancel_order(self.ids[leg])
        for leg in self.ids:
            self._await(leg, OrderStatus.CANCELLED)
        self._ok("cancel", "both CANCELLED")
        self._verify_final()

    # ── Steps ─────────────────────────────────────────────────────────────────

    def _ltp(self) -> float:
        try:
            raw = self.b.raw_read("ltp", self.key)
            self._record("ltp", {"instruments": [self.key]}, raw)
            price = float(raw[self.key]["last_price"])
        except Exception as exc:
            logger.warning("canary_rest_ltp_failed error=%s (trying the feed)", exc)
            price = self.b.get_ltp(self.cfg.symbol, self.cfg.exchange) or 0.0
        if price <= 0:
            raise CanaryAbort(f"ltp unavailable for {self.key}")
        return price

    def _tick(self) -> float:
        tick = self.b.instruments.tick_size(self.cfg.symbol, self.cfg.exchange)
        if not tick or tick <= 0:
            raise CanaryAbort(f"tick size unknown for {self.key}")
        return float(tick)

    def _place(self, leg: str, side: OrderSide, kind: OrderType, *, trigger=0.0, price=0.0):
        stamp = self.started.strftime("%H%M%S")
        tag = f"{TAG_PREFIX}{stamp}{leg[0].upper()}"  # e.g. CNRY091505S: alnum, <= 20
        oid = self.b.place_order(
            OrderRequest(
                symbol=self.cfg.symbol,
                exchange=self.cfg.exchange,
                side=side,
                order_type=kind,
                quantity=1,
                product=ProductType.MIS,
                price=price,
                trigger_price=trigger,
                tag=tag,
            )
        )
        self.ids[leg] = str(oid)
        self._ok(f"place_{leg}", f"order_id={oid} tag={tag}")

    def _await(self, leg: str, want: OrderStatus) -> dict:
        oid = self.ids[leg]
        for _ in range(self.cfg.max_polls):
            st = self.b.get_order_status(oid) or {}
            status = st.get("status")
            if status == want and not int(st.get("filled_quantity") or 0):
                return st
            if status in _BAD or int(st.get("filled_quantity") or 0):
                why = _BAD.get(status, "filled")
                reason = st.get("rejection_reason") or ""
                name = getattr(status, "name", status)
                raise CanaryAbort(f"{leg} order {oid} {why} ({name}) {reason}".strip())
            if want == OrderStatus.PENDING:
                self._guard()
            self.sleep(self.cfg.poll_interval_s)
        raise CanaryAbort(f"timeout: {leg} order {oid} never reached {want}")

    def _await_modified(self) -> None:
        field = {"sl": "trigger_price", "limit": "price"}
        for _ in range(self.cfg.max_polls):
            orders = self._orders()
            by_id = {str(o.get("order_id")): o for o in orders}
            if all(
                abs(float(by_id.get(self.ids[leg], {}).get(field[leg]) or 0) - self.prices[leg])
                < 1e-6
                for leg in self.ids
            ):
                for leg in self.ids:
                    self._await(leg, OrderStatus.PENDING)
                return
            self.sleep(self.cfg.poll_interval_s)
        raise CanaryAbort("timeout: modify never showed in orders()")

    def _guard(self) -> None:
        ltp = self._ltp()
        nearest = max(self.prices.values())
        if ltp <= nearest * (1 + self.cfg.guard_pct):
            raise CanaryAbort(f"ltp_drift: ltp={ltp} within {self.cfg.guard_pct:.0%} of {nearest}")

    def _verify_final(self) -> None:
        orders = self._orders()
        by_id = {str(o.get("order_id")): o for o in orders}
        problems = []
        for leg, oid in self.ids.items():
            o = by_id.get(oid)
            if o is None:
                problems.append(f"{leg} {oid} missing from orders()")
            elif o.get("status") != "CANCELLED" or int(o.get("filled_quantity") or 0):
                problems.append(f"{leg} {oid} status={o.get('status')}")
        left = self._working_canary(orders)
        working = {o["broker_order_id"] for o in self.b.list_open_orders()}
        left += [oid for oid in self.ids.values() if oid in working and oid not in left]
        if left:
            problems.append(f"still working: {sorted(left)}")
        if problems:
            raise CanaryAbort("; ".join(problems))
        self._ok("final", "nothing filled, nothing left working")

    def _cleanup(self) -> CheckResult:
        """Cancel every working canary-tagged order (any run's); report leftovers."""
        try:
            stray = set(self._working_canary(self._orders()))
            for oid in sorted(stray):
                try:
                    self.b.cancel_order(oid)
                except Exception as exc:
                    logger.error("canary_cleanup_cancel_failed order=%s error=%s", oid, exc)
            left = self._working_canary(self._orders()) if stray else []
        except Exception as exc:
            return CheckResult("cleanup", False, f"could not list orders: {exc}")
        if left:
            return CheckResult("cleanup", False, f"canary orders still working: {sorted(left)}")
        return CheckResult("cleanup", True, f"cancelled={sorted(stray)}")

    # ── Plumbing ──────────────────────────────────────────────────────────────

    def _orders(self) -> list[dict]:
        raw = self.b.raw_read("orders")
        self._record("orders", {}, raw)
        return list(raw or [])

    def _working_canary(self, orders: list[dict]) -> list[str]:
        ours = set(self.ids.values())
        return [
            str(o.get("order_id"))
            for o in orders
            if o.get("status") not in _DONE
            and (str(o.get("tag") or "").startswith(TAG_PREFIX) or str(o.get("order_id")) in ours)
        ]

    def _ok(self, name: str, detail: str) -> None:
        self.results.append(CheckResult(name, True, detail))

    def _record(self, call: str, params: dict, resp: object) -> None:
        self.responses.append(
            {
                "at": self.clock().replace(tzinfo=None).isoformat(),
                "call": call,
                "params": params,
                "response": resp,
            }
        )

    def _write_recording(self, report: CheckReport) -> str | None:
        try:
            day_dir = Path(self.cfg.recordings_dir) / self.started.date().isoformat()
            day_dir.mkdir(parents=True, exist_ok=True)
            path = day_dir / f"canary-{self.started.strftime('%H%M%S')}.json"
            body = {"report": report.as_dict(), "responses": self.responses}
            path.write_text(json.dumps(body, indent=2, default=str))
            return str(path)
        except Exception as exc:  # a recording problem is not a Kite problem
            logger.warning("canary_recording_failed error=%s", exc)
            return None
