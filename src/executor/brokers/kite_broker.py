from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.brokers.base import Broker
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

_KITE_STATUS_MAP: dict[str, OrderStatus] = {
    "OPEN": OrderStatus.PENDING,
    "OPEN PENDING": OrderStatus.SUBMITTING,
    "TRIGGER PENDING": OrderStatus.TRIGGERED,
    "COMPLETE": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELLED AMO": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}

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

_KITE_VALIDITY_MAP: dict[OrderValidity, str] = {
    OrderValidity.DAY: "DAY",
    OrderValidity.IOC: "IOC",
}


class KiteBroker(Broker):
    """
    Zerodha KiteBroker implementation using kiteconnect SDK.

    Handles:
    - All intraday (MIS) order placement
    - Tick feed (WebSocket) as the authoritative LTP source (Q4-1)
    - Historical OHLCV for morning batch and backtesting
    - GTT (single and OCO) for positions managed on Kite

    Session refresh: authenticate() at 09:00 IST daily via orchestrator.
    Mid-day session death: re-authenticate automatically (Loophole 6).
    """

    def __init__(self) -> None:
        self._kite = None
        self._ticker = None
        self._ltp: dict[str, float] = {}
        self._ltp_timestamp: dict[str, datetime] = {}
        self._order_callbacks: list[Callable[[dict], None]] = []
        self._tick_subscriptions: list[tuple[list[str], Callable[[str, float], None]]] = []
        self._LTP_STALENESS_SECONDS = 300  # Q4-1: 5-minute staleness threshold

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
            "price": request.price if request.order_type == OrderType.LIMIT else 0,
            "trigger_price": request.trigger_price,
            "validity": _KITE_VALIDITY_MAP[request.validity],
            "tag": request.idempotency_key[:20] if request.idempotency_key else request.tag[:20],
        }
        resp = self._kite.place_order(variety=self._kite.VARIETY_REGULAR, **params)
        return str(resp["order_id"])

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
                    "status": _KITE_STATUS_MAP.get(o["status"], OrderStatus.ERROR),
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
        self._order_callbacks.append(callback)
        if self._ticker:
            self._ticker.on_order_update = self._dispatch_order_update

    def on_tick(self, symbols: list[str], callback: Callable[[str, float], None]) -> None:
        self._tick_subscriptions.append((symbols, callback))
        self._start_ticker_if_needed(symbols)

    def get_ltp(self, symbol: str, exchange: str) -> float | None:
        now = datetime.now(IST)
        ts = self._ltp_timestamp.get(symbol)
        if ts and (now - ts).total_seconds() < self._LTP_STALENESS_SECONDS:
            return self._ltp.get(symbol)
        # Tick is stale — fall back to REST quote
        self._ensure_authenticated()
        try:
            data = self._kite.ltp([f"{exchange}:{symbol}"])
            key = f"{exchange}:{symbol}"
            return float(data[key]["last_price"])
        except Exception:
            return self._ltp.get(symbol)

    # ── GTT methods ───────────────────────────────────────────────────────────

    def place_gtt(self, request: GttRequest) -> str:
        self._ensure_authenticated()
        if request.gtt_type == GttType.SINGLE:
            params = {
                "trigger_type": self._kite.GTT_TYPE_SINGLE,
                "tradingsymbol": request.symbol,
                "exchange": request.exchange,
                "trigger_values": [request.trigger_price],
                "last_price": (
                    self.get_ltp(request.symbol, request.exchange) or request.trigger_price
                ),
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
                "last_price": (
                    self.get_ltp(request.symbol, request.exchange) or request.sl_trigger_price
                ),
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
        self._ensure_authenticated()
        self._kite.modify_gtt(trigger_id=int(broker_gtt_id), **changes)

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

    def _resolve_instrument_token(self, symbol: str, exchange: str) -> int:
        instruments = self._kite.instruments(exchange=exchange)
        for inst in instruments:
            if inst["tradingsymbol"] == symbol:
                return int(inst["instrument_token"])
        raise ValueError(f"Instrument not found: {exchange}:{symbol}")

    def _start_ticker_if_needed(self, symbols: list[str]) -> None:
        if self._ticker is not None:
            return
        try:
            from kiteconnect import KiteTicker  # type: ignore[import-untyped]

            api_key = os.environ["KITE_API_KEY"]
            access_token = os.environ["KITE_ACCESS_TOKEN"]
            self._ticker = KiteTicker(api_key, access_token)
            self._ticker.on_ticks = self._on_ticks_received
            self._ticker.on_order_update = self._dispatch_order_update
            self._ticker.connect(threaded=True)
        except Exception as exc:
            logger.error("Failed to start KiteTicker: %s", exc)

    def _on_ticks_received(self, ws: object, ticks: list[dict]) -> None:
        now = datetime.now(IST)
        for tick in ticks:
            symbol = tick.get("tradingsymbol", "")
            ltp = float(tick.get("last_price", 0))
            self._ltp[symbol] = ltp
            self._ltp_timestamp[symbol] = now
            for symbols, callback in self._tick_subscriptions:
                if symbol in symbols:
                    callback(symbol, ltp)

    def _dispatch_order_update(self, ws: object, data: dict) -> None:
        for callback in self._order_callbacks:
            callback(data)
