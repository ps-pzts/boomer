from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from executor.brokers.base import Broker
from executor.models import (
    BrokerFunds,
    BrokerName,
    BrokerPosition,
    GttRequest,
    GttStatus,
    GttType,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderValidity,
    ProductType,
)

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# fyers-apiv3 order type codes
_FYERS_ORDER_TYPE = {
    OrderType.LIMIT: 1,
    OrderType.MARKET: 2,
    OrderType.SL_LIMIT: 3,
    OrderType.SL: 4,
}

_FYERS_SIDE = {
    OrderSide.BUY: 1,
    OrderSide.SELL: -1,
}

_FYERS_PRODUCT = {
    ProductType.CNC: "CNC",
    ProductType.MIS: "INTRADAY",
}

_FYERS_VALIDITY = {
    OrderValidity.DAY: "DAY",
    OrderValidity.IOC: "IOC",
}

# Fyers order status codes → our OrderStatus
_FYERS_STATUS_MAP: dict[int, OrderStatus] = {
    1: OrderStatus.PENDING,  # Pending
    2: OrderStatus.FILLED,  # Traded
    4: OrderStatus.CANCELLED,  # Cancelled
    5: OrderStatus.REJECTED,  # Rejected
    6: OrderStatus.PARTIAL,  # Partially traded
}

# Fyers GTT type codes
_FYERS_GTT_SINGLE = 1
_FYERS_GTT_OCO = 2

# Fyers GTT status codes
_FYERS_GTT_STATUS_MAP: dict[int, GttStatus] = {
    1: GttStatus.GTT_ACTIVE,
    2: GttStatus.GTT_TRIGGERED,
    3: GttStatus.GTT_CANCELLED,
    4: GttStatus.GTT_EXPIRED,
}


class FyersBroker(Broker):
    """
    Fyers broker implementation using fyers-apiv3 SDK.

    Handles:
    - All delivery (CNC) order placement — ₹0 brokerage
    - GTC (Good Till Cancelled) orders mapping to GTT interface
    - GTT OCO for delivery stop-loss + target

    Session: OAuth2 daily token. Refresh at 09:00 IST same as Kite.
    Q4-3: Verify GTC/OCO behavior in paper trading before first live delivery trade.

    Symbol format for Fyers: "NSE:RELIANCE-EQ" (exchange:symbol-series).
    The abstraction receives plain symbols; _fyers_symbol() converts.
    """

    def __init__(self) -> None:
        self._fyers = None
        self._order_callbacks: list[Callable[[dict], None]] = []

    @property
    def broker_id(self) -> BrokerName:
        return BrokerName.FYERS

    def authenticate(self) -> None:
        from fyers_apiv3 import fyersModel  # type: ignore[import-untyped]

        client_id = os.environ["FYERS_CLIENT_ID"]
        access_token = os.environ["FYERS_ACCESS_TOKEN"]
        self._fyers = fyersModel.FyersModel(
            client_id=client_id,
            token=access_token,
            is_async=False,
            log_path="",
        )
        logger.info("FyersBroker authenticated")

    def place_order(self, request: OrderRequest) -> str:
        self._ensure_authenticated()
        data = {
            "symbol": self._fyers_symbol(request.symbol, request.exchange),
            "qty": request.quantity,
            "type": _FYERS_ORDER_TYPE[request.order_type],
            "side": _FYERS_SIDE[request.side],
            "productType": _FYERS_PRODUCT[request.product],
            "limitPrice": (
                request.price if request.order_type in (OrderType.LIMIT, OrderType.SL_LIMIT) else 0
            ),
            "stopPrice": request.trigger_price,
            "disclosedQty": 0,
            "validity": _FYERS_VALIDITY[request.validity],
            "offlineOrder": False,
        }
        resp = self._fyers.place_order(data=data)
        if resp.get("code") != 200:
            raise RuntimeError(f"Fyers order rejected: {resp.get('message', resp)}")
        return str(resp["id"])

    def modify_order(self, broker_order_id: str, changes: dict) -> str:
        self._ensure_authenticated()
        data = {"id": broker_order_id, **changes}
        self._fyers.modify_order(data=data)
        return broker_order_id

    def cancel_order(self, broker_order_id: str) -> None:
        self._ensure_authenticated()
        self._fyers.cancel_order(data={"id": broker_order_id})

    def get_order_status(self, broker_order_id: str) -> dict:
        self._ensure_authenticated()
        resp = self._fyers.orderbook()
        for o in resp.get("orderBook", []):
            if str(o.get("id")) == broker_order_id:
                return {
                    "status": _FYERS_STATUS_MAP.get(o.get("status", 0), OrderStatus.ERROR),
                    "filled_quantity": o.get("filledQty", 0),
                    "average_fill_price": o.get("tradedPrice", 0.0),
                    "rejection_reason": o.get("message", ""),
                }
        return {}

    def list_positions(self) -> list[BrokerPosition]:
        self._ensure_authenticated()
        resp = self._fyers.positions()
        positions = []
        for p in resp.get("netPositions", []):
            if p.get("netQty", 0) == 0:
                continue
            sym, exch = self._parse_fyers_symbol(p["symbol"])
            positions.append(
                BrokerPosition(
                    symbol=sym,
                    exchange=exch,
                    quantity=p["netQty"],
                    average_price=p.get("netAvg", 0.0),
                    last_price=p.get("ltp", 0.0),
                    product=ProductType.MIS,
                    broker_id=BrokerName.FYERS,
                )
            )
        return positions

    def list_holdings(self) -> list[BrokerPosition]:
        self._ensure_authenticated()
        resp = self._fyers.holdings()
        holdings = []
        for h in resp.get("holdings", []):
            if h.get("quantity", 0) <= 0:
                continue
            sym, exch = self._parse_fyers_symbol(h["symbol"])
            holdings.append(
                BrokerPosition(
                    symbol=sym,
                    exchange=exch,
                    quantity=h["quantity"],
                    average_price=h.get("costPrice", 0.0),
                    last_price=h.get("ltp", 0.0),
                    product=ProductType.CNC,
                    broker_id=BrokerName.FYERS,
                )
            )
        return holdings

    def get_funds(self) -> BrokerFunds:
        self._ensure_authenticated()
        resp = self._fyers.funds()
        fund_limit = resp.get("fund_limit", [])
        cash = 0.0
        used = 0.0
        for item in fund_limit:
            if item.get("title") == "Total Balance":
                cash = float(item.get("equityAmount", 0))
            if item.get("title") == "Utilized Amount":
                used = float(item.get("equityAmount", 0))
        return BrokerFunds(available_cash=cash - used, used_margin=used, broker_id=BrokerName.FYERS)

    def on_order_update(self, callback: Callable[[dict], None]) -> None:
        # Fyers v3 WebSocket order updates handled via FyersDataSocket separately.
        # For now, callbacks are stored; polling reconciliation covers the gap.
        self._order_callbacks.append(callback)

    def on_tick(self, symbols: list[str], callback: Callable[[str, float], None]) -> None:
        # Tick feed for delivery positions is not primary — Kite WebSocket is.
        # Fyers tick is a no-op here; KiteBroker.on_tick() is the LTP source (Q4-1).
        pass

    # ── GTT methods ───────────────────────────────────────────────────────────
    # Q4-3: Fyers GTT API — verify OCO behavior in paper trading before live use.

    def place_gtt(self, request: GttRequest) -> str:
        self._ensure_authenticated()
        expiry = (datetime.now(IST) + timedelta(days=request.valid_days)).strftime("%Y-%m-%d")

        if request.gtt_type == GttType.SINGLE:
            data = {
                "symbol": self._fyers_symbol(request.symbol, request.exchange),
                "triggerType": _FYERS_GTT_SINGLE,
                "qty": request.quantity,
                "side": _FYERS_SIDE[OrderSide.SELL],
                "type": _FYERS_ORDER_TYPE[OrderType.LIMIT],
                "limitPrice": request.limit_price,
                "stopPrice": request.trigger_price,
                "validity": "DAY",
                "productType": "CNC",
                "expiryTime": expiry,
            }
        else:
            # OCO: two legs — SL trigger and target trigger
            data = {
                "symbol": self._fyers_symbol(request.symbol, request.exchange),
                "triggerType": _FYERS_GTT_OCO,
                "qty": request.quantity,
                "side": _FYERS_SIDE[OrderSide.SELL],
                "productType": "CNC",
                "expiryTime": expiry,
                "leg1": {
                    "type": _FYERS_ORDER_TYPE[OrderType.LIMIT],
                    "limitPrice": request.sl_limit_price,
                    "stopPrice": request.sl_trigger_price,
                },
                "leg2": {
                    "type": _FYERS_ORDER_TYPE[OrderType.LIMIT],
                    "limitPrice": request.target_limit_price,
                    "stopPrice": request.target_trigger_price,
                },
            }

        resp = self._fyers.place_gtt(data=data)
        if resp.get("code") != 200:
            raise RuntimeError(f"Fyers GTT rejected: {resp.get('message', resp)}")
        return str(resp.get("data", {}).get("id", ""))

    def modify_gtt(self, broker_gtt_id: str, changes: dict) -> None:
        self._ensure_authenticated()
        data = {"id": broker_gtt_id, **changes}
        self._fyers.modify_gtt(data=data)

    def cancel_gtt(self, broker_gtt_id: str) -> None:
        self._ensure_authenticated()
        self._fyers.cancel_gtt(data={"id": broker_gtt_id})

    def get_gtt(self, broker_gtt_id: str) -> dict:
        self._ensure_authenticated()
        resp = self._fyers.get_gtt(data={"id": broker_gtt_id})
        return resp.get("data", {})

    def list_gtts(self) -> list[dict]:
        self._ensure_authenticated()
        resp = self._fyers.list_gtt()
        return resp.get("data", {}).get("gttList", [])

    def get_ltp(self, symbol: str, exchange: str) -> float | None:
        self._ensure_authenticated()
        fyers_sym = self._fyers_symbol(symbol, exchange)
        resp = self._fyers.quotes(data={"symbols": fyers_sym})
        quotes = resp.get("d", [])
        if quotes:
            return float(quotes[0].get("v", {}).get("lp", 0))
        return None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _ensure_authenticated(self) -> None:
        if self._fyers is None:
            raise RuntimeError("FyersBroker not authenticated — call authenticate() first")

    @staticmethod
    def _fyers_symbol(symbol: str, exchange: str) -> str:
        """Convert NSE:RELIANCE → NSE:RELIANCE-EQ (Fyers requires series suffix)."""
        if "-" in symbol:
            return f"{exchange}:{symbol}"
        return f"{exchange}:{symbol}-EQ"

    @staticmethod
    def _parse_fyers_symbol(fyers_sym: str) -> tuple[str, str]:
        """Convert NSE:RELIANCE-EQ → (RELIANCE, NSE)."""
        parts = fyers_sym.split(":", 1)
        exchange = parts[0] if len(parts) > 1 else "NSE"
        sym_series = parts[-1]
        symbol = sym_series.rsplit("-", 1)[0] if "-" in sym_series else sym_series
        return symbol, exchange
