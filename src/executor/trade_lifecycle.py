"""TradeSupervisor: drives each intraday trade through the Trade machine (#45, SM-2).

States and triggers: `executor/trade_machine.py` (designs/state-machines.md §5).
Every state change goes through `statemachine.transition()`, atomically with
its event and with any bracket fields that change alongside it.

Rules this module enforces (CLAUDE.md §10, design §7):
- No entry without a valid stop: long only, 0 < stop < entry < target, ATR > 0.
- On entry fill → PROTECTING: an SL-M stop and a LIMIT target for the filled
  quantity. OPEN only once the stop is confirmed resting at the broker; if it
  can't be placed or is rejected → stop_failed → EXITING at market + CRITICAL (L9).
- OCO is emulated (Kite GTTs don't support MIS): when a leg fills, the other is
  cancelled and the trade settles from the *net filled quantity across all its
  orders*. Any race (both legs filling in a fast move) ends in a corrective
  market order: sell the remainder, or buy back the oversold quantity + CRITICAL.
- A stop that disappears without filling → stop_lost → market exit + CRITICAL.
- Stops only move UP: breakeven at +1R, then 1×ATR below the high (#71,
  executor/trailing.py). They can never be widened.

Restart safety (SM4): all state lives in `intraday_brackets` + `orders`. A trade
found in PROTECTING reuses a stop it already submitted (found by its
idempotency key) instead of placing a second one. A second resting stop would
later fire into a short.

Threading: reconcile() polls; call it from orchestrator task threads only (the
SQLite connection must not cross threads; paper fills arrive on the feed thread).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from executor.models import (
    OrderRecord,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from executor.trade_store import BracketStore
from executor.trailing import Trailer

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

TRACK = "intraday"
ENTRY_VALID_MINUTES = 30  # design L1
_DEAD = frozenset({OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.ERROR})
_RESTING = frozenset({OrderStatus.PENDING, OrderStatus.TRIGGERED, OrderStatus.PARTIAL})


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    exchange: str
    quantity: int
    entry_price: float  # LIMIT price (worst fill)
    stop_price: float
    target_price: float
    atr: float
    trade_plan_id: str | None = None
    recommendation_id: str | None = None
    # Set for a breakout entry: a stop-limit BUY (Kite "SL") that only becomes a
    # LIMIT at entry_price once price trades at the trigger (#69). None = LIMIT.
    entry_trigger: float | None = None


def validate(req: TradeRequest) -> str | None:
    """None if the trade may be entered, else the rule it breaks."""
    if req.quantity < 1:
        return "quantity_below_one"
    if not (0 < req.stop_price < req.entry_price < req.target_price):
        return "stop_entry_target_not_ordered"  # long only: stop < entry < target
    if req.atr <= 0:
        return "atr_not_positive"
    if req.entry_trigger is not None and not (
        req.stop_price < req.entry_trigger <= req.entry_price
    ):
        return "stop_trigger_limit_not_ordered"  # stop < trigger <= limit
    return None


class TradeSupervisor:
    def __init__(
        self,
        db: sqlite3.Connection,
        order_manager: object,
        position_manager: object,
        alerter: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
        price: Callable[[str], float | None] | None = None,
    ) -> None:
        self._store = BracketStore(db, clock)
        # Trailing stop (#71) needs a live price; without one stops stay fixed.
        self._trailer = Trailer(self._store, order_manager, price) if price else None
        self._om = order_manager
        self._pm = position_manager
        self._alerter = alerter
        self._clock = clock
        self._price = price

    # ── Public API ────────────────────────────────────────────────────────────

    def open_trade(self, req: TradeRequest) -> str:
        """Validate, submit the MIS entry (LIMIT, or stop-limit when `entry_trigger` is
        set), persist the bracket. Returns bracket_id.

        Raises ValueError if the plan is invalid; PreTradeCheckError (from
        OrderManager) if the entry order is refused. Nothing is persisted then.
        """
        rule = validate(req)
        if rule:
            from observability.emit import emit

            emit(
                "rejection",
                "trade_rejected",
                severity="warn",
                symbol=req.symbol,
                rule=rule,
                inputs=vars(req),
            )
            raise ValueError(f"trade rejected: {rule}")
        bid = str(uuid.uuid4())
        now = self._clock()
        entry_id = self._submit(
            req.symbol,
            req.exchange,
            OrderSide.BUY,
            OrderType.LIMIT if req.entry_trigger is None else OrderType.SL_LIMIT,
            req.quantity,
            f"{bid}-entry",
            price=req.entry_price,
            trigger=req.entry_trigger or 0.0,
            plan=req.trade_plan_id,
            rec=req.recommendation_id,
        )
        self._store.insert(
            bid, req, entry_id, now, entry_expires_at=now + timedelta(minutes=ENTRY_VALID_MINUTES)
        )
        return bid

    def reconcile(self) -> None:
        """Advance every active trade one step. Safe to call any time, any number of times."""
        try:  # an order whose placement outcome was unknown must not keep resting (#97)
            self._om.cancel_lost_orders()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("cancel_lost_orders_failed")
        for b in self._store.active():
            try:
                step = {
                    "entry_pending": self._step_entry,
                    "protecting": self._step_protecting,
                    "open": self._step_open,
                    "exiting": self._step_exiting,
                }[b["state"]]
                step(b)
            except Exception as exc:
                logger.exception("trade_reconcile_failed bracket=%s", b["bracket_id"])
                self._critical(f"Trade reconcile failed — {b['symbol']}", str(exc))
        self._mark_to_market()

    def _mark_to_market(self) -> None:
        """Give open positions their last price and unrealised P&L (#106).

        A symbol with no tick is left as it was (never zeroed). update_ltp only touches
        is_open=1 rows, so a position closed this pass keeps its exit price and P&L.
        """
        if self._price is None:
            return
        try:
            for symbol in {b["symbol"] for b in self._store.active() if b["position_id"]}:
                ltp = self._price(symbol)
                if ltp:
                    self._pm.update_ltp(symbol, float(ltp))  # type: ignore[attr-defined]
        except Exception:  # a display refresh must never break trade management
            logger.exception("mark_to_market_failed")

    def square_off_all(self) -> None:
        """15:14 IST: flatten everything. Never leaves an MIS position."""
        self.reconcile()  # settle anything that already filled
        for b in self._store.active():
            try:
                if b["state"] == "entry_pending":
                    self._end_entry(b, trigger="square_off")
                    b = self._store.load(b["bracket_id"])
                if b["state"] in ("protecting", "open"):
                    for leg in ("stop_order_id", "target_order_id"):
                        if b[leg]:
                            self._om.cancel(b[leg])
                    self._store.advance(
                        b, "square_off", rule="square_off_1514", exit_reason="squareoff"
                    )
                    self._settle(self._store.load(b["bracket_id"]))
            except Exception as exc:
                logger.exception("squareoff_failed bracket=%s", b["bracket_id"])
                self._critical(f"Square-off failed — {b['symbol']}", str(exc))

    def active_count(self) -> int:
        return len(self._store.active())

    def net_positions(self) -> dict[str, int]:
        """Net filled quantity per symbol across active trades, from fills (§4.5)."""
        out: dict[str, int] = {}
        for b in self._store.active():
            out[b["symbol"]] = out.get(b["symbol"], 0) + self._net_and_cash(b)[0]
        return {s: q for s, q in out.items() if q}

    def open_legs(self) -> list[tuple[str, str, str, str | None, bool]]:
        """(bracket_id, symbol, leg, order_id, resting) for every OPEN trade's stop
        and target, as the broker reports them now (§4.5)."""
        out = []
        for b in self._store.active():
            if b["state"] != "open":
                continue
            for leg in ("stop", "target"):
                oid = b[f"{leg}_order_id"]
                resting = bool(oid) and self._om.sync_status(oid).status in _RESTING
                if oid or leg == "stop":
                    out.append((b["bracket_id"], b["symbol"], leg, oid, resting))
        return out

    # ── ENTRY_PENDING ─────────────────────────────────────────────────────────

    def _step_entry(self, b: sqlite3.Row) -> None:
        entry = self._om.sync_status(b["entry_order_id"])
        if entry.status == OrderStatus.FILLED:
            self._on_entry_filled(b, entry)
        elif entry.status in _DEAD:
            self._end_entry(b, trigger="entry_rejected", entry=entry)
        elif entry.filled_quantity > 0:  # #99: shares exist: cancel the rest, protect them
            self._end_entry(b, trigger="entry_partial")
        elif self._clock() >= datetime.fromisoformat(b["entry_expires_at"]):
            self._end_entry(b, trigger="entry_expired")

    def _end_entry(self, b: sqlite3.Row, trigger: str, entry: OrderRecord | None = None) -> None:
        """Entry is over (expired, rejected, square-off). Keep any partial fill (L12)."""
        if entry is None or entry.status not in _DEAD:
            entry = self._om.cancel(b["entry_order_id"])  # records a fill that raced it
            if entry.status in _RESTING:  # #98: cancel not confirmed; the rest could still fill
                logger.warning("entry_cancel_unconfirmed bracket=%s", b["bracket_id"])
                return
        if entry.filled_quantity > 0:
            self._on_entry_filled(b, entry)
            return
        self._store.advance(
            b,
            trigger,
            rule=trigger,
            inputs={"entry_status": entry.status},
            exit_reason=trigger,
            closed_at=self._now(),
        )

    def _on_entry_filled(self, b: sqlite3.Row, entry: OrderRecord) -> None:
        qty = entry.filled_quantity
        # Opens flagged unprotected; cleared only once the stop is confirmed.
        position_id = self._pm.open_position(
            symbol=b["symbol"],
            exchange=b["exchange"],
            track=TRACK,
            bucket_id=TRACK,
            broker_id=entry.broker_id,
            quantity=qty,
            average_entry_price=entry.average_fill_price,
            stop_loss_price=b["stop_price"],
            target_price=b["target_price"],
            atr_at_entry=b["atr"],
            entry_order_id=entry.order_id,
            trade_plan_id=b["trade_plan_id"],
            recommendation_id=b["recommendation_id"],
        )
        self._store.advance(
            b,
            "entry_filled",
            rule="entry_order_filled",
            inputs={"qty": qty, "price": entry.average_fill_price},
            filled_quantity=qty,
            position_id=position_id,
        )
        self._step_protecting(self._store.load(b["bracket_id"]))

    # ── PROTECTING ────────────────────────────────────────────────────────────

    def _step_protecting(self, b: sqlite3.Row) -> None:
        stop_id = (
            b["stop_order_id"]
            or self._store.existing_order_id(b, "stop")
            or self._place_leg(b, "stop")
        )
        target_id = (
            b["target_order_id"]
            or self._store.existing_order_id(b, "target")
            or self._place_leg(b, "target")
        )
        self._store.set(b["bracket_id"], stop_order_id=stop_id, target_order_id=target_id)
        stop = self._om.sync_status(stop_id) if stop_id else None
        b = self._store.load(b["bracket_id"])
        if stop is None or stop.status not in _RESTING:
            if stop is not None and stop.status == OrderStatus.FILLED:
                self._store.advance(b, "stop_confirmed", rule="stop_filled_immediately")
                self._step_open(self._store.load(b["bracket_id"]))
                return
            self._unprotected(
                b, trigger="stop_failed", reason=f"stop_{stop.status if stop else 'not_submitted'}"
            )
            return
        self._pm.clear_unprotected(b["position_id"])
        if target_id is None:
            self._warn(
                f"Target not placed — {b['symbol']}", "Stop is in place; exits on stop or 15:14."
            )
        self._store.advance(
            b,
            "stop_confirmed",
            rule="stop_resting_at_broker",
            inputs={
                "stop": b["stop_price"],
                "target": b["target_price"],
                "stop_order_id": stop_id,
                "target_order_id": target_id,
            },
        )

    def _place_leg(self, b: sqlite3.Row, leg: str) -> str | None:
        qty = b["filled_quantity"]
        kind = OrderType.SL if leg == "stop" else OrderType.LIMIT
        price = 0.0 if leg == "stop" else b["target_price"]
        trigger = b["stop_price"] if leg == "stop" else 0.0
        try:
            return self._submit(
                b["symbol"],
                b["exchange"],
                OrderSide.SELL,
                kind,
                qty,
                f"{b['bracket_id']}-{leg}",
                price=price,
                trigger=trigger,
            )
        except Exception as exc:
            logger.error("leg_submit_failed bracket=%s leg=%s error=%s", b["bracket_id"], leg, exc)
            return None

    # ── OPEN ──────────────────────────────────────────────────────────────────

    def _step_open(self, b: sqlite3.Row) -> None:
        stop = self._om.sync_status(b["stop_order_id"])
        target = self._om.sync_status(b["target_order_id"]) if b["target_order_id"] else None
        legs = [leg for leg in (stop, target) if leg is not None]
        if any(leg.status == OrderStatus.FILLED for leg in legs):
            for leg in legs:  # OCO: cancel whatever is still resting
                if leg.status in _RESTING:
                    self._om.cancel(leg.order_id)
            filled = "stop" if stop.status == OrderStatus.FILLED else "target"
            self._store.advance(
                b,
                "leg_filled",
                rule=f"{filled}_hit",
                inputs={
                    "stop_status": stop.status,
                    "target_status": target.status if target else None,
                },
                exit_reason=f"{filled}_hit",
            )
            self._settle(self._store.load(b["bracket_id"]))
        elif stop.status in _DEAD:
            self._unprotected(b, trigger="stop_lost", reason=f"stop_{stop.status}")
        elif self._trailer is not None and stop.status in _RESTING:
            entry = self._om.sync_status(b["entry_order_id"])
            self._trailer.step(b, entry.average_fill_price)

    # ── EXITING ───────────────────────────────────────────────────────────────

    def _step_exiting(self, b: sqlite3.Row) -> None:
        exits = json.loads(b["exit_order_ids"])
        if not exits:  # crashed between entering EXITING and placing the exit
            self._settle(b)
            return
        last = self._om.sync_status(exits[-1])
        if last.status in _DEAD:
            self._critical(f"Exit order {last.status} — {b['symbol']}", "Retrying at market.")
            self._store.advance(b, "exit_failed", rule=f"exit_{last.status}", severity="critical")
            self._settle(self._store.load(b["bracket_id"]))
        elif last.status == OrderStatus.FILLED:
            self._settle(b)

    def _settle(self, b: sqlite3.Row) -> None:
        """Bring the net position to zero from actual fills, or close if it already is."""
        still_resting = self._cancel_resting_legs(b)
        net, cash = self._net_and_cash(b)
        if net == 0:
            if still_resting:  # #98: never close over a leg that could still fill
                self._critical(
                    f"Leg still working — {b['symbol']}",
                    f"Cancel of {still_resting} not confirmed; trade stays EXITING and retries.",
                )
                return
            self._close(b, realised=cash)
            return
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        if net < 0:
            self._critical(
                f"Oversold {b['symbol']} by {-net} — buying back",
                "Both exit legs filled before the other could be cancelled.",
            )
        n = self._store.exit_attempts(b) + 1  # #101: failed attempts burn their key too
        exit_id = self._submit(
            b["symbol"],
            b["exchange"],
            side,
            OrderType.MARKET,
            abs(net),
            f"{b['bracket_id']}-exit{n}",
        )
        self._store.advance(
            b,
            "corrective_order",
            rule="net_position_not_zero",
            inputs={"net": net, "side": side.value, "order_id": exit_id},
            severity="critical" if net < 0 else "info",
            exit_order_ids=json.dumps([*json.loads(b["exit_order_ids"]), exit_id]),
        )

    def _cancel_resting_legs(self, b: sqlite3.Row) -> list[str]:
        """Cancel whatever stop/target still rests (an exit must never race a leg).
        Returns the order ids the broker still shows working afterwards (#98)."""
        left = []
        for leg in ("stop_order_id", "target_order_id"):
            oid = b[leg]
            if (
                oid
                and self._om.sync_status(oid).status in _RESTING
                and self._om.cancel(oid).status in _RESTING
            ):
                left.append(oid)
        return left

    def _net_and_cash(self, b: sqlite3.Row) -> tuple[int, float]:
        ids = [
            b["entry_order_id"],
            b["stop_order_id"],
            b["target_order_id"],
            *json.loads(b["exit_order_ids"]),
        ]
        net, cash = 0, 0.0
        for oid in filter(None, ids):
            o = self._om.sync_status(oid)
            sign = 1 if o.side == OrderSide.BUY else -1
            net += sign * o.filled_quantity
            cash -= sign * o.filled_quantity * o.average_fill_price
        return net, cash

    def _close(self, b: sqlite3.Row, realised: float) -> None:
        entry = self._om.sync_status(b["entry_order_id"])
        exit_price = entry.average_fill_price + realised / b["filled_quantity"]
        self._pm.close_position(b["position_id"], exit_price, realised)
        self._store.advance(
            b,
            "flat",
            rule="net_position_zero",
            inputs={"realised_pnl": round(realised, 2)},
            realised_pnl=realised,
            closed_at=self._now(),
        )
        self._info(
            f"Trade closed — {b['symbol']}", f"{b['exit_reason']}, realised P&L ₹{realised:,.2f}"
        )

    def _unprotected(self, b: sqlite3.Row, trigger: str, reason: str) -> None:
        """L9: never leave an MIS position without a stop. Exit at market now."""
        self._pm.mark_unprotected(b["position_id"])
        self._critical(f"UNPROTECTED {b['symbol']} — exiting at market", reason)
        if b["target_order_id"]:
            self._om.cancel(b["target_order_id"])
        self._store.advance(b, trigger, rule=reason, severity="critical", exit_reason=reason)
        self._settle(self._store.load(b["bracket_id"]))

    # ── Plumbing ──────────────────────────────────────────────────────────────

    def _submit(
        self,
        symbol,
        exchange,
        side,
        order_type,
        qty,
        key,
        price=0.0,
        trigger=0.0,
        plan=None,
        rec=None,
    ) -> str:
        return self._om.submit(
            OrderRequest(
                symbol=symbol,
                exchange=exchange,
                side=side,
                order_type=order_type,
                quantity=qty,
                product=ProductType.MIS,
                price=price,
                trigger_price=trigger,
                idempotency_key=key,
                tag="b7",
                trade_plan_id=plan,
                recommendation_id=rec,
            ),
            TRACK,
        )

    def _now(self) -> str:
        return self._clock().isoformat()

    def _critical(self, title: str, body: str) -> None:
        logger.error("trade_critical %s: %s", title, body)
        if self._alerter:
            self._alerter.critical(title, body, source_task_id="trade_supervisor")

    def _warn(self, title: str, body: str) -> None:
        logger.warning("trade_warn %s: %s", title, body)
        if self._alerter:
            self._alerter.warn(title, body, source_task_id="trade_supervisor")

    def _info(self, title: str, body: str) -> None:
        if self._alerter:
            self._alerter.info(title, body, source_task_id="trade_supervisor")
