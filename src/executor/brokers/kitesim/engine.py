"""KiteSim's exchange: order book, matching, GTT triggers, postbacks, state (#86).

`KiteSim` (sim.py) is the kiteconnect-shaped facade over this engine. Three
single choke points exist on purpose, for hostile mode (#87):

  _run(fn)          every API call and price event: the lock, then postback
                    delivery and persistence once the outermost call finishes
  _advance(o, s)    every broker-side order state change (fills included)
  _emit(o)          every postback; delivered by `_deliver` outside the lock
"""

from __future__ import annotations

import copy
import logging
import random
import threading
from collections.abc import Callable
from datetime import datetime

from kiteconnect import exceptions as kex

from executor.brokers.kitesim import gtt as gtts
from executor.brokers.kitesim import rules
from executor.brokers.kitesim.config import KiteSimConfig, bar_path
from executor.brokers.kitesim.instruments import InstrumentBook
from executor.brokers.kitesim.payloads import new_order, postback, ts
from executor.brokers.kitesim.portfolio import Portfolio, added_exposure

logger = logging.getLogger(__name__)

STATE_FORMAT = "kitesim"
STATE_VERSION = 1
WORKING = frozenset({"OPEN", "TRIGGER PENDING"})
FINAL = frozenset({"COMPLETE", "CANCELLED", "CANCELLED AMO", "REJECTED"})
_PRE_EXCHANGE = frozenset({"PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "REJECTED"})
EOD_MESSAGE = "Your order has been cancelled by the exchange at market close."


class SimEngine:
    def __init__(
        self,
        instruments: InstrumentBook | Callable[[str], list[dict]] | None,
        cash: float,
        clock: Callable[[], datetime],
        config: KiteSimConfig | None = None,
    ) -> None:
        self.config = config or KiteSimConfig()
        if isinstance(instruments, InstrumentBook):
            self._instruments = instruments
        else:
            self._instruments = InstrumentBook(instruments)
        self._clock = clock
        self._lock = threading.RLock()
        self._rng = random.Random(self.config.seed)
        self._book = Portfolio(cash, self.config.mis_margin_rate, self.config.cnc_margin_rate)
        self._orders: dict[str, dict] = {}
        self._history: dict[str, list[dict]] = {}
        self._gtts: dict[int, dict] = {}
        self._ltp: dict[str, float] = {}
        self._ref: dict[str, float] = {}  # circuit reference: first price, then prev close
        self._close: dict[str, float] = {}  # previous close (positions' close_price)
        self._seq = 0
        self._gtt_seq = 0
        self._listeners: list[Callable[[dict], None]] = []
        self._outbox: list[dict] = []
        self._depth = 0
        self._dirty = False
        self._event_time: datetime | None = None
        self.holdings_book: list[dict] = []
        # interceptor(call, params, proceed) -> result: the API hook (#87).
        self.interceptor: Callable | None = None
        # postback_gate(payload, deliver): decides when/how often a postback reaches
        # the listeners (delay, duplicate, drop, reorder: hostile mode #87).
        self.postback_gate: Callable[[dict, Callable[[dict], None]], None] | None = None
        # on_change(state) after any call that changed the book (paper persistence).
        self.on_change: Callable[[dict], None] | None = None

    # ── choke points ──────────────────────────────────────────────────────────

    def _run(self, fn: Callable[[], object]) -> object:
        outbox: list[dict] = []
        state: dict | None = None
        try:
            with self._lock:
                self._depth += 1
                try:
                    return fn()
                finally:
                    self._depth -= 1
                    if self._depth == 0:
                        outbox, self._outbox = self._outbox, []
                        if self._dirty and self.on_change is not None:
                            state = self._state()
                        self._dirty = False
        finally:
            if state is not None:
                try:
                    self.on_change(state)  # type: ignore[misc]
                except Exception:  # persistence must never break an order path
                    logger.exception("kitesim_on_change_failed")
            for payload in outbox:
                self._deliver(payload)

    def _dispatch(self, call: str, params: dict) -> object:
        impl = getattr(self, f"_api_{call}")

        def proceed(**override: object) -> object:
            return impl(**(override or params))

        if self.interceptor is None:
            return self._run(proceed)
        return self._run(lambda: self.interceptor(call, dict(params), proceed))  # type: ignore[misc]

    def _advance(self, order: dict, status: str, **fields: object) -> None:
        """The one place an order's broker-side state changes. A grown
        filled_quantity books the new shares into positions."""
        old_filled, old_avg = order["filled_quantity"], order["average_price"]
        order.update(fields)
        order["status"] = status
        now = ts(self._now())
        order["exchange_update_timestamp"] = now
        if status not in _PRE_EXCHANGE and order["exchange_order_id"] is None:
            order["exchange_order_id"] = f"11{self._rng.randrange(10**13):014d}"
            order["exchange_timestamp"] = now
        delta = order["filled_quantity"] - old_filled
        if delta > 0:
            price = (
                order["average_price"] * order["filled_quantity"] - old_avg * old_filled
            ) / delta
            pos = self._book.position(
                order["exchange"],
                order["tradingsymbol"],
                order["product"],
                order["instrument_token"],
            )
            pos.apply(order["transaction_type"], delta, round(price, 4))
        order["_blocked"] = (
            0.0
            if status in FINAL
            else order.get("_per_share", 0.0) * max(0, order["pending_quantity"])
        )
        self._history.setdefault(order["order_id"], []).append(copy.deepcopy(order))
        self._dirty = True
        self._emit(order)

    def _emit(self, order: dict) -> None:
        self._outbox.append(postback(order, self.config.user_id))

    def _deliver(self, payload: dict) -> None:
        if self.postback_gate is not None:
            self.postback_gate(payload, self._deliver_now)
        else:
            self._deliver_now(payload)

    def _deliver_now(self, payload: dict) -> None:
        for cb in list(self._listeners):
            try:
                cb(copy.deepcopy(payload))
            except Exception:  # one bad listener must not starve the others
                logger.exception("kitesim_postback_listener_failed")

    def subscribe_postbacks(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def advance(self, order_id: str, status: str, **fields: object) -> None:
        """Force a broker-side state change (tests; hostile mode #87)."""
        self._run(lambda: self._advance(self._order(order_id), status, **fields))

    # ── prices ────────────────────────────────────────────────────────────────

    def on_price(
        self,
        symbol: str,
        exchange: str,
        ltp: float,
        ts: datetime | None = None,
        continuous: bool = False,
    ) -> None:
        """One market price (a live tick: a jump, not continuous)."""
        self._run(lambda: self._price_event(symbol, exchange, float(ltp), continuous, ts))

    def on_bar(self, bar: object, exchange: str = "NSE", ts: datetime | None = None) -> None:
        """Walk a bar: open (a gap from the last price), nearer extreme, farther
        extreme, close (continuous moves)."""
        path = bar_path(bar.open, bar.high, bar.low, bar.close)  # type: ignore[attr-defined]

        def walk() -> None:
            for i, p in enumerate(path):
                self._price_event(bar.symbol, exchange, float(p), i > 0, ts)  # type: ignore[attr-defined]

        self._run(walk)

    def end_of_day(self) -> None:
        """Market close: DAY orders still working are cancelled by the exchange;
        today's last price becomes the next day's reference (previous close)."""

        def close() -> None:
            for o in self._working():
                self._advance(
                    o,
                    "CANCELLED",
                    status_message=EOD_MESSAGE,
                    cancelled_quantity=o["pending_quantity"],
                    pending_quantity=0,
                )
            self._close.update(self._ltp)
            self._ref = dict(self._ltp)

        self._run(close)

    def _price_event(
        self, symbol: str, exchange: str, p: float, continuous: bool, at: datetime | None
    ) -> None:
        key = f"{exchange}:{symbol}"
        self._event_time = at
        try:
            self._ltp[key] = p
            self._ref.setdefault(key, p)
            for o in self._working(key):
                self._match(o, p, continuous, fresh=False)
            for g in list(self._gtts.values()):
                cond = g["condition"]
                if f"{cond['exchange']}:{cond['tradingsymbol']}" != key:
                    continue
                leg = gtts.fired_leg(g, p)
                if leg is not None:
                    self._fire_gtt(g, leg)
        finally:
            self._event_time = None

    def _working(self, key: str | None = None) -> list[dict]:
        out = [
            o
            for o in self._orders.values()
            if o["status"] in WORKING
            and (key is None or f"{o['exchange']}:{o['tradingsymbol']}" == key)
        ]
        return sorted(out, key=lambda o: o["_seq"])

    # ── matching ──────────────────────────────────────────────────────────────

    def _match(self, o: dict, p: float, continuous: bool, fresh: bool) -> None:
        side, kind = o["transaction_type"], o["order_type"]
        if o["status"] == "TRIGGER PENDING":
            trig = o["trigger_price"]
            if not rules.stop_crossed(side, trig, p):
                return
            self._advance(o, "OPEN")  # triggered: SL-M is now a market order, SL a limit
            if kind == "SL-M":
                self._fill(o, rules.stop_fill_price(side, trig, p, continuous))
            else:
                at = trig if continuous else p
                if rules.limit_marketable(side, o["price"], at):
                    self._fill(o, at)
            return
        if o["status"] != "OPEN":
            return
        if kind in ("MARKET", "SL-M"):
            self._fill(o, p)
        elif rules.limit_marketable(side, o["price"], p):
            self._fill(o, p if fresh else o["price"])

    def _fill(self, o: dict, price: float) -> None:
        qty = o["pending_quantity"]
        cap = self.config.liquidity_per_tick
        if cap is not None:
            qty = min(qty, cap)
        if qty <= 0:
            return
        filled = o["filled_quantity"] + qty
        avg = (o["average_price"] * o["filled_quantity"] + price * qty) / filled
        pending = o["quantity"] - filled
        self._advance(
            o,
            "COMPLETE" if pending == 0 else "OPEN",
            filled_quantity=filled,
            pending_quantity=pending,
            average_price=round(avg, 4),
        )

    # ── orders ────────────────────────────────────────────────────────────────

    def _now(self) -> datetime:
        return self._event_time or self._clock()

    def _order(self, order_id: str) -> dict:
        o = self._orders.get(str(order_id))
        if o is None:
            raise kex.OrderException("Couldn't find that `order_id`.")
        return o

    def _instrument(self, symbol: str, exchange: str) -> dict:
        row = self._instruments.row(symbol, exchange)
        if row is None or not float(row.get("tick_size") or 0) > 0:
            raise kex.InputException(
                "The instrument you are placing an order for has either expired or does not exist."
            )
        return row

    def _create(self, params: dict, row: dict) -> dict:
        """Accept validated params as a new order: RMS checks, then working."""
        self._seq += 1
        now = self._now()
        oid = f"{now:%y%m%d}{self._seq:09d}"
        o = new_order(
            order_id=oid,
            guid=f"{self._rng.randrange(10**5):05d}X{self._seq:09d}",
            user=self.config.user_id,
            token=int(row["instrument_token"]),
            now=ts(now),
            seq=self._seq,
            **params,
        )
        self._orders[oid] = o
        key = f"{o['exchange']}:{o['tradingsymbol']}"
        ltp = self._ltp.get(key)
        held = self._book.net_quantity(o["exchange"], o["tradingsymbol"], o["product"])
        adds = added_exposure(held, o["transaction_type"], o["quantity"])
        ref = rules.reference_price(o, ltp)
        rate = self._book.rate(o["product"])
        o["_per_share"] = (ref or 0.0) * rate * adds / o["quantity"]
        reason = self._rms(o, adds, ref, key)
        if reason is not None:
            msg, raw = reason
            self._advance(
                o, "REJECTED", status_message=msg, status_message_raw=raw, pending_quantity=0
            )
            return o
        self._advance(o, "TRIGGER PENDING" if o["order_type"] in rules.STOP_TYPES else "OPEN")
        if ltp is not None:
            self._match(o, ltp, continuous=False, fresh=True)
        if o["validity"] == "IOC" and o["status"] in WORKING:
            self._advance(
                o, "CANCELLED", cancelled_quantity=o["pending_quantity"], pending_quantity=0
            )
        return o

    def _rms(self, o: dict, adds: int, ref: float | None, key: str) -> tuple[str, str] | None:
        cfg = self.config
        user = cfg.user_id

        def rms(msg: str | None) -> tuple[str, str] | None:
            return None if msg is None else (msg, f"RMS:{msg} for entity account-{user}")

        found = rms(rules.freeze_rejection(o["quantity"], cfg.freeze_quantity))
        if found is None and adds:
            found = rms(rules.mis_cutoff_rejection(o["product"], self._now(), cfg.mis_cutoff))
        if found is None:
            found = rms(rules.circuit_rejection(o, self._ref.get(key), cfg.circuit_pct))
        if found is None and cfg.check_margin and adds and ref is not None:
            required = ref * adds * self._book.rate(o["product"])
            found = rules.margin_rejection(required, self._available(), user)
        return found

    def _available(self) -> float:
        return self._book.available(self._order_blocked(), self._ltp)

    def _order_blocked(self) -> float:
        return sum(o.get("_blocked", 0.0) for o in self._orders.values() if o["status"] in WORKING)

    def _fire_gtt(self, g: dict, leg_index: int) -> None:
        now = ts(self._now())
        g["status"], g["updated_at"] = "triggered", now
        leg = g["orders"][leg_index]
        row = self._instrument(leg["tradingsymbol"], leg["exchange"])
        o = self._create(
            {
                "variety": "regular",
                "exchange": leg["exchange"],
                "symbol": leg["tradingsymbol"],
                "side": leg["transaction_type"],
                "order_type": leg["order_type"],
                "product": leg["product"],
                "quantity": int(leg["quantity"]),
                "price": float(leg["price"]),
                "trigger_price": 0.0,
                "validity": "DAY",
                "tag": None,
                "market_protection": None,
            },
            row,
        )
        ok = o["status"] != "REJECTED"
        leg["result"] = gtts.result(
            leg,
            o["order_id"],
            "success" if ok else "failed",
            "" if ok else str(o["status_message"]),
            now,
            self.config.user_id,
        )
        self._dirty = True

    # ── state ─────────────────────────────────────────────────────────────────

    def _state(self) -> dict:
        version, internal, gauss = self._rng.getstate()
        return copy.deepcopy(
            {
                "format": STATE_FORMAT,
                "version": STATE_VERSION,
                "book": self._book.to_state(),
                "orders": self._orders,
                "history": self._history,
                "gtts": {str(k): v for k, v in self._gtts.items()},
                "ltp": self._ltp,
                "ref": self._ref,
                "close": self._close,
                "seq": self._seq,
                "gtt_seq": self._gtt_seq,
                "rng": [version, list(internal), gauss],
            }
        )

    def to_state(self) -> dict:
        """Everything needed to rebuild this simulator (JSON-safe)."""
        with self._lock:
            return self._state()

    def load_state(self, state: dict | None) -> bool:
        """Restore `to_state()` output. Anything else (a pre-#86 paper book,
        a newer format) is ignored with a log line; returns whether it loaded."""
        if not state:
            return False
        if state.get("format") != STATE_FORMAT or state.get("version") != STATE_VERSION:
            logger.warning(
                "kitesim_state_ignored format=%r version=%r (not a KiteSim v%d book)",
                state.get("format"),
                state.get("version"),
                STATE_VERSION,
            )
            return False
        with self._lock:
            s = copy.deepcopy(state)
            self._book.load_state(s["book"])
            self._orders, self._history = s["orders"], s["history"]
            self._gtts = {int(k): v for k, v in s["gtts"].items()}
            self._ltp, self._ref, self._close = s["ltp"], s["ref"], s["close"]
            self._seq, self._gtt_seq = s["seq"], s["gtt_seq"]
            version, internal, gauss = s["rng"]
            self._rng.setstate((version, tuple(internal), gauss))
        return True
