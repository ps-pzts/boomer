"""TradeSupervisor: owns every intraday trade from entry to exit (design §3.5, #45).

    entry_pending ──fill──► open ──one leg fills──► (net flat) closed
         │                   │                  └► (net ≠ 0) exiting ──► closed
         └─expired/rejected──► cancelled

Rules this module enforces (CLAUDE.md §10, design §7):
- No entry without a valid stop: long only, 0 < stop < entry < target.
- On entry fill: an SL-M stop and a LIMIT target for the filled quantity, at once.
  If the stop can't be placed or is rejected, exit at market + CRITICAL (L9).
- OCO is emulated (Kite GTTs don't support MIS): when a leg fills, the other is
  cancelled. Decisions use the *net filled quantity* across all of the trade's
  orders, so a race (both legs filling in a fast move) ends in a corrective
  order: sell any remainder, buy back any oversold quantity + CRITICAL.
- A protective leg that disappears without filling (cancelled by someone else,
  expired) means unprotected: exit at market.
- Stops are never moved here, so they can never be widened.

Threading: reconcile() polls order status. It is called from orchestrator task
threads, never from broker callbacks: in paper mode fills arrive on the feed
thread and the SQLite connection must not cross threads. The resulting OCO lag
(up to the reconcile interval) is why the oversold guard exists.

Every state change is logged as `trade_event` with the rule and inputs (§9).
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

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

TRACK = "intraday"
ENTRY_VALID_MINUTES = 30  # design L1
_DEAD = frozenset(
    {OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.ERROR}
)
_ACTIVE_STATES = ("entry_pending", "open", "exiting")


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    exchange: str
    quantity: int
    entry_price: float  # LIMIT price
    stop_price: float
    target_price: float
    atr: float
    trade_plan_id: str | None = None
    recommendation_id: str | None = None


def validate(req: TradeRequest) -> str | None:
    """None if the trade may be entered, else the rule it breaks."""
    if req.quantity < 1:
        return "quantity_below_one"
    if not (0 < req.stop_price < req.entry_price < req.target_price):
        return "stop_entry_target_not_ordered"  # long only: stop < entry < target
    if req.atr <= 0:
        return "atr_not_positive"
    return None


class TradeSupervisor:
    def __init__(
        self,
        db: sqlite3.Connection,
        order_manager: object,
        position_manager: object,
        alerter: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
    ) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row
        self._om = order_manager
        self._pm = position_manager
        self._alerter = alerter
        self._clock = clock

    # ── Public API ────────────────────────────────────────────────────────────

    def open_trade(self, req: TradeRequest) -> str:
        """Validate, submit the MIS LIMIT entry, persist the bracket. Returns bracket_id.

        Raises ValueError if the plan is invalid; PreTradeCheckError (from
        OrderManager) if the entry order is refused. Nothing is persisted then.
        """
        rule = validate(req)
        if rule:
            self._event(None, "entry_rejected", rule=rule, symbol=req.symbol, req=vars(req))
            raise ValueError(f"trade rejected: {rule}")
        bracket_id = str(uuid.uuid4())
        now = self._clock()
        entry_id = self._submit(
            req.symbol,
            req.exchange,
            OrderSide.BUY,
            OrderType.LIMIT,
            req.quantity,
            f"{bracket_id}-entry",
            price=req.entry_price,
            plan=req.trade_plan_id,
            rec=req.recommendation_id,
        )
        self._db.execute(
            """INSERT INTO intraday_brackets (bracket_id, session_date, symbol, exchange,
                   planned_quantity, entry_price, stop_price, target_price, atr,
                   entry_order_id, entry_expires_at, state, trade_plan_id,
                   recommendation_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'entry_pending',?,?,?,?)""",
            (
                bracket_id,
                now.date().isoformat(),
                req.symbol,
                req.exchange,
                req.quantity,
                req.entry_price,
                req.stop_price,
                req.target_price,
                req.atr,
                entry_id,
                (now + timedelta(minutes=ENTRY_VALID_MINUTES)).isoformat(),
                req.trade_plan_id,
                req.recommendation_id,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self._db.commit()
        self._event(bracket_id, "entry_submitted", rule="plan_valid", req=vars(req))
        return bracket_id

    def reconcile(self) -> None:
        """Advance every active trade one step. Safe to call any time, any number of times."""
        for b in self._active():
            try:
                {
                    "entry_pending": self._step_entry,
                    "open": self._step_open,
                    "exiting": self._step_exiting,
                }[b["state"]](b)
            except Exception as exc:
                logger.exception("trade_reconcile_failed bracket=%s", b["bracket_id"])
                self._critical(f"Trade reconcile failed — {b['symbol']}", str(exc))

    def square_off_all(self) -> None:
        """15:14 IST: flatten everything. Unfilled entries are cancelled; open trades
        have their legs cancelled and exit at market. Never leaves an MIS position."""
        self.reconcile()  # settle anything that already filled
        for b in self._active():
            try:
                if b["state"] == "entry_pending":
                    self._expire_entry(b, reason="squareoff")
                    b = self._load(b["bracket_id"])
                if b["state"] == "open":
                    for leg in ("stop_order_id", "target_order_id"):
                        if b[leg]:
                            self._om.cancel(b[leg])
                    self._settle(self._load(b["bracket_id"]), reason="squareoff")
            except Exception as exc:
                logger.exception("squareoff_failed bracket=%s", b["bracket_id"])
                self._critical(f"Square-off failed — {b['symbol']}", str(exc))

    def active_count(self) -> int:
        return len(self._active())

    # ── State steps ───────────────────────────────────────────────────────────

    def _step_entry(self, b: sqlite3.Row) -> None:
        entry = self._om.sync_status(b["entry_order_id"])
        if entry.status == OrderStatus.FILLED:
            self._on_entry_filled(b, entry)
        elif entry.status in _DEAD:
            self._finish_unfilled_entry(b, entry, reason=f"entry_{entry.status}")
        elif self._clock() >= datetime.fromisoformat(b["entry_expires_at"]):
            self._expire_entry(b, reason="entry_expired")

    def _expire_entry(self, b: sqlite3.Row, reason: str) -> None:
        entry = self._om.cancel(b["entry_order_id"])  # records a fill that raced the cancel
        if entry.status == OrderStatus.FILLED:
            self._on_entry_filled(b, entry)
        else:
            self._finish_unfilled_entry(b, entry, reason=reason)

    def _finish_unfilled_entry(self, b: sqlite3.Row, entry: OrderRecord, reason: str) -> None:
        if entry.filled_quantity > 0:  # partial fill, remainder dead (L12)
            self._on_entry_filled(b, entry)
            return
        self._update(b["bracket_id"], state="cancelled", exit_reason=reason, closed_at=self._now())
        self._event(b["bracket_id"], "entry_cancelled", rule=reason)

    def _on_entry_filled(self, b: sqlite3.Row, entry: OrderRecord) -> None:
        qty = entry.filled_quantity
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
        )  # opens flagged unprotected until the stop is confirmed
        self._update(b["bracket_id"], state="open", filled_quantity=qty, position_id=position_id)
        self._event(b["bracket_id"], "entry_filled", qty=qty, price=entry.average_fill_price)
        b = self._load(b["bracket_id"])

        stop_id = self._try_submit(b, OrderType.SL, qty, "stop", trigger=b["stop_price"])
        target_id = self._try_submit(b, OrderType.LIMIT, qty, "target", price=b["target_price"])
        self._update(b["bracket_id"], stop_order_id=stop_id, target_order_id=target_id)
        stop = self._om.sync_status(stop_id) if stop_id else None
        if stop is None or stop.status in _DEAD:
            self._unprotected(self._load(b["bracket_id"]), "stop_rejected")
            return
        self._pm.clear_unprotected(position_id)
        if target_id is None:
            self._warn(
                f"Target not placed — {b['symbol']}", "Stop is in place; exits on stop or 15:14."
            )
        self._event(b["bracket_id"], "protected", stop=b["stop_price"], target=b["target_price"])

    def _step_open(self, b: sqlite3.Row) -> None:
        stop = self._om.sync_status(b["stop_order_id"])
        target = self._om.sync_status(b["target_order_id"]) if b["target_order_id"] else None
        legs = [leg for leg in (stop, target) if leg is not None]
        if any(leg.status == OrderStatus.FILLED for leg in legs):
            for leg in legs:  # OCO: cancel whatever is still resting
                if leg.status not in (OrderStatus.FILLED, *_DEAD):
                    self._om.cancel(leg.order_id)
            filled = "stop" if stop.status == OrderStatus.FILLED else "target"
            self._settle(self._load(b["bracket_id"]), reason=f"{filled}_hit")
        elif stop.status in _DEAD:
            self._unprotected(b, f"stop_{stop.status}")

    def _step_exiting(self, b: sqlite3.Row) -> None:
        last = self._om.sync_status(json.loads(b["exit_order_ids"])[-1])
        if last.status == OrderStatus.FILLED or last.status in _DEAD:
            if last.status in _DEAD:
                self._critical(f"Exit order {last.status} — {b['symbol']}", "Retrying at market.")
            self._settle(b, reason=b["exit_reason"] or "exit")

    # ── Settlement and guards ─────────────────────────────────────────────────

    def _settle(self, b: sqlite3.Row, reason: str) -> None:
        """Bring the net position to zero from actual fills, or close the trade if it is."""
        net, cash = self._net_and_cash(b)
        if net == 0:
            self._close(b, realised=cash, reason=reason)
            return
        side = OrderSide.SELL if net > 0 else OrderSide.BUY
        if net < 0:
            self._critical(
                f"Oversold {b['symbol']} by {-net} — buying back",
                f"Both exit legs filled before the other could be cancelled ({reason}).",
            )
        n = len(json.loads(b["exit_order_ids"])) + 1
        exit_id = self._submit(
            b["symbol"],
            b["exchange"],
            side,
            OrderType.MARKET,
            abs(net),
            f"{b['bracket_id']}-exit{n}",
        )
        ids = [*json.loads(b["exit_order_ids"]), exit_id]
        self._update(
            b["bracket_id"], state="exiting", exit_order_ids=json.dumps(ids), exit_reason=reason
        )
        self._event(b["bracket_id"], "market_exit", rule=reason, side=side.value, qty=abs(net))

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

    def _close(self, b: sqlite3.Row, realised: float, reason: str) -> None:
        exit_price = b["entry_price"]
        if b["filled_quantity"]:
            # average exit implied by the realised P&L (exact for a single-leg exit)
            entry = self._om.sync_status(b["entry_order_id"])
            exit_price = entry.average_fill_price + realised / b["filled_quantity"]
        if b["position_id"]:
            self._pm.close_position(b["position_id"], exit_price, realised)
        self._update(
            b["bracket_id"],
            state="closed",
            realised_pnl=realised,
            exit_reason=reason,
            closed_at=self._now(),
        )
        self._event(b["bracket_id"], "closed", rule=reason, realised_pnl=round(realised, 2))
        self._info(f"Trade closed — {b['symbol']}", f"{reason}, realised P&L ₹{realised:,.2f}")

    def _unprotected(self, b: sqlite3.Row, reason: str) -> None:
        """L9: never leave an MIS position without a stop. Exit at market now."""
        if b["position_id"]:
            self._pm.mark_unprotected(b["position_id"])
        self._critical(f"UNPROTECTED {b['symbol']} — exiting at market", reason)
        if b["target_order_id"]:
            self._om.cancel(b["target_order_id"])
        self._settle(self._load(b["bracket_id"]), reason=reason)

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

    def _try_submit(self, b, order_type, qty, leg, price=0.0, trigger=0.0) -> str | None:
        try:
            return self._submit(
                b["symbol"],
                b["exchange"],
                OrderSide.SELL,
                order_type,
                qty,
                f"{b['bracket_id']}-{leg}",
                price=price,
                trigger=trigger,
            )
        except Exception as exc:
            logger.error("leg_submit_failed bracket=%s leg=%s error=%s", b["bracket_id"], leg, exc)
            return None

    def _active(self) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(_ACTIVE_STATES))
        q = f"SELECT * FROM intraday_brackets WHERE state IN ({marks}) ORDER BY created_at"
        return self._db.execute(q, _ACTIVE_STATES).fetchall()

    def _load(self, bracket_id: str) -> sqlite3.Row:
        return self._db.execute(
            "SELECT * FROM intraday_brackets WHERE bracket_id=?", (bracket_id,)
        ).fetchone()

    def _update(self, bracket_id: str, **fields: object) -> None:
        fields["updated_at"] = self._now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._db.execute(
            f"UPDATE intraday_brackets SET {cols} WHERE bracket_id=?",
            (*fields.values(), bracket_id),
        )
        self._db.commit()

    def _now(self) -> str:
        return self._clock().isoformat()

    def _event(self, bracket_id: str | None, event: str, **inputs: object) -> None:
        logger.info("trade_event bracket=%s event=%s inputs=%s", bracket_id, event, inputs)

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
