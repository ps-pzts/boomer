from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.brokers.base import Broker
from executor.brokers.feeds.base import MarketDataFeed
from executor.brokers.kite_contract import FINAL_ORDER_STATUSES, IN_FLIGHT_ORDER_STATUSES
from executor.brokers.kite_instruments import InstrumentCache
from executor.models import (
    BrokerFunds,
    BrokerName,
    BrokerPosition,
    GttRequest,
    GttType,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    PriceBar,
    ProductType,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Kite order status -> ours (#73). Kite reports many in-flight statuses while an
# order is being placed / modified / cancelled; those mean "no change yet" and
# must never become a terminal ERROR (that read as a lost stop and exited a
# healthy trade at market).
_KITE_FINAL: dict[str, OrderStatus] = {
    "COMPLETE": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELLED AMO": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}
_KITE_IN_FLIGHT = IN_FLIGHT_ORDER_STATUSES
_KITE_STOP_TYPES = frozenset({"SL", "SL-M"})


def _kite_tag(raw: str) -> str:
    """Kite tags are alphanumeric, max 20 chars; ours carry UUID hyphens (#73)."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "")[:20]


def map_kite_status(status: str, order_type: str, filled: int, quantity: int) -> OrderStatus | None:
    """Our status for a Kite order, or None = unchanged (in flight / unknown).

    TRIGGER PENDING  stop waiting for its trigger            -> PENDING (resting)
    OPEN             LIMIT/MARKET resting                    -> PENDING
                     SL/SL-M after the trigger fired          -> TRIGGERED
                     any order with part of it filled         -> PARTIAL
    """
    if status in _KITE_FINAL:
        return _KITE_FINAL[status]
    if status == "TRIGGER PENDING":
        return OrderStatus.PENDING
    if status == "OPEN":
        if 0 < filled < quantity:
            return OrderStatus.PARTIAL
        return OrderStatus.TRIGGERED if order_type in _KITE_STOP_TYPES else OrderStatus.PENDING
    if status not in _KITE_IN_FLIGHT:
        logger.warning("kite_unknown_order_status status=%r (treated as unchanged)", status)
    return None


_KITE_ORDER_TYPE_MAP: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL-M",
    OrderType.SL_LIMIT: "SL",
}

_KITE_SIDE_MAP: dict[OrderSide, str] = {
    OrderSide.BUY: "BUY",
    OrderSide.SELL: "SELL",
}

_KITE_PRODUCT_MAP: dict[ProductType, str] = {
    ProductType.MIS: "MIS",
    ProductType.CNC: "CNC",
}

# Order types whose `price` field Kite reads: LIMIT, and SL (stop-limit) once triggered.
_PRICED_ORDER_TYPES = frozenset({OrderType.LIMIT, OrderType.SL_LIMIT})
# Order types Kite requires `market_protection` on for API orders (#43).
_MARKET_PROTECTED_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.SL})
_AUTO_MARKET_PROTECTION = -1

_KITE_VALIDITY_MAP: dict[OrderValidity, str] = {
    OrderValidity.DAY: "DAY",
    OrderValidity.IOC: "IOC",
}


# Kite statuses after which an order can no longer fill (#65).
_KITE_DONE = FINAL_ORDER_STATUSES


class KiteBroker(Broker):
    """
    Zerodha KiteBroker implementation using kiteconnect SDK.

    Handles:
    - All intraday (MIS) order placement
    - LTP: fresh ticks from the attached MarketDataFeed (KiteFeed owns the WebSocket),
      falling back to REST
    - Historical OHLCV for morning batch and backtesting
    - GTT (single and OCO) for positions managed on Kite

    Session refresh: authenticate() at 09:00 IST daily via orchestrator.
    Mid-day session death: re-authenticate automatically (Loophole 6).
    """

    def __init__(self, feed: MarketDataFeed | None = None) -> None:
        self._kite = None
        self._feed = feed
        self._ltp: dict[str, float] = {}
        self._ltp_timestamp: dict[str, datetime] = {}
        self._LTP_STALENESS_SECONDS = 300  # Q4-1: 5-minute staleness threshold
        self.instruments = InstrumentCache(self._load_instruments)

    def attach_feed(self, feed: MarketDataFeed) -> None:
        self._feed = feed

    @property
    def broker_id(self) -> BrokerName:
        return BrokerName.KITE

    def authenticate(self) -> None:
        from kiteconnect import KiteConnect  # type: ignore[import-untyped]

        api_key = os.environ["KITE_API_KEY"]
        access_token = os.environ["KITE_ACCESS_TOKEN"]
        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(access_token)
        logger.info("KiteBroker authenticated")

    def place_order(self, request: OrderRequest) -> str:
        self._ensure_authenticated()
        params = {
            "tradingsymbol": request.symbol,
            "exchange": request.exchange,
            "transaction_type": _KITE_SIDE_MAP[request.side],
            "order_type": _KITE_ORDER_TYPE_MAP[request.order_type],
            "quantity": request.quantity,
            "product": _KITE_PRODUCT_MAP[request.product],
            "price": request.price if request.order_type in _PRICED_ORDER_TYPES else 0,
            "trigger_price": request.trigger_price,
            "validity": _KITE_VALIDITY_MAP[request.validity],
            "tag": _kite_tag(request.idempotency_key or request.tag),
        }
        if request.order_type in _MARKET_PROTECTED_ORDER_TYPES:
            # Kite rejects API MARKET / SL-M orders without market protection (#43);
            # -1 lets Kite apply its automatic protection band.
            params["market_protection"] = _AUTO_MARKET_PROTECTION
        resp = self._kite.place_order(variety=self._kite.VARIETY_REGULAR, **params)
        # kiteconnect returns the order id itself (a str), not the response dict.
        # Indexing it raised TypeError AFTER Kite had accepted the order (#73).
        return str(resp["order_id"] if isinstance(resp, dict) else resp)

    def modify_order(self, broker_order_id: str, changes: dict) -> str:
        self._ensure_authenticated()
        self._kite.modify_order(
            variety=self._kite.VARIETY_REGULAR,
            order_id=broker_order_id,
            **changes,
        )
        return broker_order_id

    def cancel_order(self, broker_order_id: str) -> None:
        self._ensure_authenticated()
        self._kite.cancel_order(variety=self._kite.VARIETY_REGULAR, order_id=broker_order_id)

    def get_order_status(self, broker_order_id: str) -> dict:
        self._ensure_authenticated()
        orders = self._kite.orders()
        for o in orders:
            if str(o["order_id"]) == broker_order_id:
                return {
                    "status": map_kite_status(
                        o["status"],
                        o.get("order_type", ""),
                        int(o.get("filled_quantity") or 0),
                        int(o.get("quantity") or 0),
                    ),
                    "filled_quantity": o.get("filled_quantity", 0),
                    "average_fill_price": o.get("average_price", 0.0),
                    "rejection_reason": o.get("status_message", ""),
                }
        return {}

    def list_positions(self) -> list[BrokerPosition]:
        self._ensure_authenticated()
        raw = self._kite.positions()
        return [
            BrokerPosition(
                symbol=p["tradingsymbol"],
                exchange=p["exchange"],
                quantity=p["quantity"],
                average_price=p["average_price"],
                last_price=p.get("last_price", 0.0),
                product=ProductType.MIS,
                broker_id=BrokerName.KITE,
            )
            for p in raw.get("day", [])
            if p["quantity"] != 0
        ]

    def list_holdings(self) -> list[BrokerPosition]:
        self._ensure_authenticated()
        raw = self._kite.holdings()
        return [
            BrokerPosition(
                symbol=h["tradingsymbol"],
                exchange=h["exchange"],
                quantity=h["quantity"],
                average_price=h["average_price"],
                last_price=h.get("last_price", 0.0),
                product=ProductType.CNC,
                broker_id=BrokerName.KITE,
            )
            for h in raw
            if h["quantity"] > 0
        ]

    def get_funds(self) -> BrokerFunds:
        self._ensure_authenticated()
        data = self._kite.margins(segment="equity")
        return BrokerFunds(
            available_cash=data["available"]["cash"],
            used_margin=data["utilised"]["debits"],
            broker_id=BrokerName.KITE,
        )

    def on_order_update(self, callback: Callable[[dict], None]) -> None:
        # Order updates arrive on the same WebSocket as ticks, which the feed owns.
        if self._feed is None:
            raise RuntimeError("KiteBroker has no feed attached — order updates unavailable")
        self._feed.on_order_update(callback)

    def on_tick(self, symbols: list[str], callback: Callable[[str, float], None]) -> None:
        if self._feed is None:
            raise RuntimeError("KiteBroker has no feed attached — ticks unavailable")
        wanted = set(symbols)
        self._feed.subscribe(symbols)
        self._feed.on_tick(lambda t: callback(t.symbol, t.ltp) if t.symbol in wanted else None)

    def get_ltp(self, symbol: str, exchange: str) -> float | None:
        now = datetime.now(IST)
        # Tier 0: fresh tick from the live feed
        tick = self._feed.latest(symbol) if self._feed else None
        if tick and (now - tick.received_at).total_seconds() < self._LTP_STALENESS_SECONDS:
            return tick.ltp
        ts = self._ltp_timestamp.get(symbol)
        if ts and (now - ts).total_seconds() < self._LTP_STALENESS_SECONDS:
            return self._ltp.get(symbol)
        self._ensure_authenticated()
        # Tier 1: REST LTP endpoint (requires market data subscription)
        try:
            data = self._kite.ltp([f"{exchange}:{symbol}"])
            price = float(data[f"{exchange}:{symbol}"]["last_price"])
            self._ltp[symbol] = price
            self._ltp_timestamp[symbol] = now
            return price
        except Exception:
            pass
        # Tier 2: holdings + positions — available on base plan, covers held stocks
        try:
            for h in self._kite.holdings():
                if h["tradingsymbol"] == symbol and h.get("last_price", 0) > 0:
                    return float(h["last_price"])
            for p in self._kite.positions().get("day", []):
                if p["tradingsymbol"] == symbol and p.get("last_price", 0) > 0:
                    return float(p["last_price"])
        except Exception:
            pass
        # Tier 3: last known price (stale tick or old REST value — better than None)
        if tick:
            return tick.ltp
        return self._ltp.get(symbol)

    # ── GTT methods ───────────────────────────────────────────────────────────

    def place_gtt(self, request: GttRequest) -> str:
        self._ensure_authenticated()
        last_price = self.get_ltp(request.symbol, request.exchange)
        if not last_price:
            raise RuntimeError(
                f"Cannot place GTT for {request.symbol}: LTP unavailable. "
                "Ensure the tick feed is subscribed or the market data plan is active."
            )
        if request.gtt_type == GttType.SINGLE:
            params = {
                "trigger_type": self._kite.GTT_TYPE_SINGLE,
                "tradingsymbol": request.symbol,
                "exchange": request.exchange,
                "trigger_values": [request.trigger_price],
                "last_price": last_price,
                "orders": [
                    {
                        "transaction_type": "SELL",
                        "quantity": request.quantity,
                        "product": "CNC",
                        "order_type": "LIMIT",
                        "price": request.limit_price,
                    }
                ],
            }
        else:
            params = {
                "trigger_type": self._kite.GTT_TYPE_OCO,
                "tradingsymbol": request.symbol,
                "exchange": request.exchange,
                "trigger_values": [request.sl_trigger_price, request.target_trigger_price],
                "last_price": last_price,
                "orders": [
                    {
                        "transaction_type": "SELL",
                        "quantity": request.quantity,
                        "product": "CNC",
                        "order_type": "LIMIT",
                        "price": request.sl_limit_price,
                    },
                    {
                        "transaction_type": "SELL",
                        "quantity": request.quantity,
                        "product": "CNC",
                        "order_type": "LIMIT",
                        "price": request.target_limit_price,
                    },
                ],
            }
        resp = self._kite.place_gtt(**params)
        return str(resp["trigger_id"])

    def modify_gtt(self, broker_gtt_id: str, changes: dict) -> None:
        """Apply `{"legN": {"stopPrice", "limitPrice"}}` changes (GttManager's format).

        Kite's modify_gtt replaces the whole trigger, so the current GTT is read
        back and resent with only the named legs changed (#84: passing `leg1`
        straight to the SDK raised TypeError on every live trail)."""
        self._ensure_authenticated()
        current = self._kite.get_gtt(trigger_id=int(broker_gtt_id))
        cond = current["condition"]
        trigger_values = list(cond["trigger_values"])
        orders = [
            {k: o[k] for k in ("transaction_type", "quantity", "product", "order_type", "price")}
            for o in current["orders"]
        ]
        for i in range(len(orders)):
            leg = changes.get(f"leg{i + 1}")
            if leg:
                trigger_values[i] = leg["stopPrice"]
                orders[i]["price"] = leg["limitPrice"]
        last_price = self.get_ltp(cond["tradingsymbol"], cond["exchange"])
        if not last_price:  # same guard as place_gtt: never a stale or made-up price
            raise RuntimeError(f"Cannot modify GTT {broker_gtt_id}: LTP unavailable.")
        self._kite.modify_gtt(
            trigger_id=int(broker_gtt_id),
            trigger_type=current["type"],
            tradingsymbol=cond["tradingsymbol"],
            exchange=cond["exchange"],
            trigger_values=trigger_values,
            last_price=last_price,
            orders=orders,
        )

    def cancel_gtt(self, broker_gtt_id: str) -> None:
        self._ensure_authenticated()
        self._kite.delete_gtt(trigger_id=int(broker_gtt_id))

    def get_gtt(self, broker_gtt_id: str) -> dict:
        self._ensure_authenticated()
        return self._kite.get_gtt(trigger_id=int(broker_gtt_id))

    def list_gtts(self) -> list[dict]:
        self._ensure_authenticated()
        return self._kite.get_gtts()

    def get_historical_ohlcv(
        self,
        symbol: str,
        exchange: str,
        from_date: str,
        to_date: str,
        interval: str = "day",
    ) -> list[PriceBar]:
        # Q4-4: single instrument per request; caller manages 3 req/s rate limit
        self._ensure_authenticated()
        instrument_token = self._resolve_instrument_token(symbol, exchange)
        data = self._kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
        )
        return [
            PriceBar(
                symbol=symbol,
                date=str(row["date"].date()) if isinstance(row["date"], datetime) else row["date"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
            )
            for row in data
        ]

    # ── Private ───────────────────────────────────────────────────────────────

    def _ensure_authenticated(self) -> None:
        if self._kite is None:
            raise RuntimeError("KiteBroker not authenticated — call authenticate() first")

    def _load_instruments(self, exchange: str) -> list[dict]:
        self._ensure_authenticated()
        return self._kite.instruments(exchange=exchange)

    def list_open_orders(self) -> list[dict]:
        """Kite orders not yet COMPLETE / CANCELLED / REJECTED (#65). In-flight
        statuses (MODIFY PENDING, ...) count as working."""
        self._ensure_authenticated()
        out = []
        for o in self._kite.orders():
            if o.get("status") in _KITE_DONE:
                continue
            out.append(
                {
                    "broker_order_id": str(o["order_id"]),
                    "symbol": o.get("tradingsymbol", ""),
                    "exchange": o.get("exchange", ""),
                    "side": str(o.get("transaction_type", "")).lower(),
                    "order_type": o.get("order_type", ""),
                    "status": o.get("status", ""),
                }
            )
        return out

    def tick_size(self, symbol: str, exchange: str) -> float:
        """From Kite's instrument dump; 0.05 only if the instrument isn't listed (#75)."""
        try:
            tick = self.instruments.tick_size(symbol, exchange)
        except Exception as exc:  # dump unavailable: log, fall back
            logger.warning("tick_size_unavailable %s:%s error=%s", exchange, symbol, exc)
            tick = None
        return tick or 0.05

    def _resolve_instrument_token(self, symbol: str, exchange: str) -> int:
        return self.instruments.token(symbol, exchange)
