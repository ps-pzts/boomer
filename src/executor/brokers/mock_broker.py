from __future__ import annotations

import uuid
from collections.abc import Callable

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
    PriceBar,
    ProductType,
)


class MockBroker(Broker):
    """
    Broker implementation for backtesting and unit tests.

    Fills orders deterministically based on price bars injected via
    set_price_bar(). No real network calls are ever made.

    Same-code principle: backtester uses this broker; live trading uses
    KiteBroker or FyersBroker — the executor code is identical.
    """

    def __init__(self, initial_cash: float = 100_000.0) -> None:
        self._cash = initial_cash
        self._orders: dict[str, dict] = {}
        self._gtts: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}
        self._current_bars: dict[str, PriceBar] = {}
        self._order_callbacks: list[Callable[[dict], None]] = []
        self._tick_callbacks: list[tuple[list[str], Callable[[str, float], None]]] = []
        self._holdings: dict[str, dict] = {}

    @property
    def broker_id(self) -> BrokerName:
        return BrokerName.MOCK

    def authenticate(self) -> None:
        pass

    def set_price_bar(self, bar: PriceBar) -> None:
        """Advance simulation clock to this bar and check resting orders."""
        self._current_bars[bar.symbol] = bar
        self._emit_ticks(bar)
        self._process_resting_orders(bar)
        self._process_resting_gtts(bar)

    def set_cash(self, amount: float) -> None:
        self._cash = amount

    # ── Order methods ─────────────────────────────────────────────────────────

    def place_order(self, request: OrderRequest) -> str:
        broker_order_id = str(uuid.uuid4())
        order = {
            "broker_order_id": broker_order_id,
            "symbol": request.symbol,
            "exchange": request.exchange,
            "side": request.side,
            "order_type": request.order_type,
            "quantity": request.quantity,
            "filled_quantity": 0,
            "product": request.product,
            "price": request.price,
            "trigger_price": request.trigger_price,
            "average_fill_price": 0.0,
            "status": OrderStatus.PENDING,
            "rejection_reason": "",
        }
        self._orders[broker_order_id] = order

        bar = self._current_bars.get(request.symbol)
        if bar and self._would_fill_immediately(request, bar):
            fill_price = self._compute_fill_price(request, bar)
            self._fill_order(broker_order_id, request.quantity, fill_price)

        return broker_order_id

    def modify_order(self, broker_order_id: str, changes: dict) -> str:
        if broker_order_id in self._orders:
            self._orders[broker_order_id].update(changes)
        return broker_order_id

    def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id in self._orders:
            order = self._orders[broker_order_id]
            if order["status"] not in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                order["status"] = OrderStatus.CANCELLED
                self._notify_order_update(order)

    def get_order_status(self, broker_order_id: str) -> dict:
        return self._orders.get(broker_order_id, {})

    def list_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                symbol=p["symbol"],
                exchange=p["exchange"],
                quantity=p["quantity"],
                average_price=p["average_price"],
                last_price=(
                    self._current_bars[p["symbol"]].close
                    if p["symbol"] in self._current_bars else p["average_price"]
                ),
                product=ProductType.MIS,
                broker_id=BrokerName.MOCK,
            )
            for p in self._positions.values()
            if p["product"] == ProductType.MIS
        ]

    def list_holdings(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                symbol=h["symbol"],
                exchange=h["exchange"],
                quantity=h["quantity"],
                average_price=h["average_price"],
                last_price=(
                    self._current_bars[h["symbol"]].close
                    if h["symbol"] in self._current_bars else h["average_price"]
                ),
                product=ProductType.CNC,
                broker_id=BrokerName.MOCK,
            )
            for h in self._holdings.values()
        ]

    def get_funds(self) -> BrokerFunds:
        return BrokerFunds(available_cash=self._cash, used_margin=0.0, broker_id=BrokerName.MOCK)

    def on_order_update(self, callback: Callable[[dict], None]) -> None:
        self._order_callbacks.append(callback)

    def on_tick(self, symbols: list[str], callback: Callable[[str, float], None]) -> None:
        self._tick_callbacks.append((symbols, callback))

    # ── GTT methods ───────────────────────────────────────────────────────────

    def place_gtt(self, request: GttRequest) -> str:
        broker_gtt_id = str(uuid.uuid4())
        self._gtts[broker_gtt_id] = {
            "broker_gtt_id": broker_gtt_id,
            "symbol": request.symbol,
            "exchange": request.exchange,
            "gtt_type": request.gtt_type,
            "quantity": request.quantity,
            "trigger_price": request.trigger_price,
            "limit_price": request.limit_price,
            "sl_trigger_price": request.sl_trigger_price,
            "sl_limit_price": request.sl_limit_price,
            "target_trigger_price": request.target_trigger_price,
            "target_limit_price": request.target_limit_price,
            "status": GttStatus.GTT_ACTIVE,
            "triggered_order_id": None,
        }
        return broker_gtt_id

    def modify_gtt(self, broker_gtt_id: str, changes: dict) -> None:
        if broker_gtt_id in self._gtts:
            self._gtts[broker_gtt_id].update(changes)

    def cancel_gtt(self, broker_gtt_id: str) -> None:
        if broker_gtt_id in self._gtts:
            self._gtts[broker_gtt_id]["status"] = GttStatus.GTT_CANCELLED

    def get_gtt(self, broker_gtt_id: str) -> dict:
        return self._gtts.get(broker_gtt_id, {})

    def list_gtts(self) -> list[dict]:
        return list(self._gtts.values())

    def get_historical_ohlcv(
        self,
        symbol: str,
        exchange: str,
        from_date: str,
        to_date: str,
        interval: str = "day",
    ) -> list[PriceBar]:
        return []

    def get_ltp(self, symbol: str, exchange: str) -> float | None:
        bar = self._current_bars.get(symbol)
        return bar.close if bar else None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _would_fill_immediately(self, request: OrderRequest, bar: PriceBar) -> bool:
        if request.order_type == OrderType.MARKET:
            return True
        if request.order_type == OrderType.LIMIT:
            return (request.side == OrderSide.BUY and bar.low <= request.price) or (
                request.side == OrderSide.SELL and bar.high >= request.price
            )
        return False

    def _compute_fill_price(self, request: OrderRequest, bar: PriceBar) -> float:
        if request.order_type == OrderType.MARKET:
            return bar.open
        return request.price

    def _fill_order(self, broker_order_id: str, quantity: int, fill_price: float) -> None:
        order = self._orders[broker_order_id]
        order["filled_quantity"] = quantity
        order["average_fill_price"] = fill_price
        order["status"] = OrderStatus.FILLED
        side_sign = 1 if order["side"] == OrderSide.BUY else -1
        self._cash -= side_sign * quantity * fill_price
        self._notify_order_update(order)

    def _process_resting_orders(self, bar: PriceBar) -> None:
        for broker_order_id, order in self._orders.items():
            if order["status"] != OrderStatus.PENDING:
                continue
            if order["symbol"] != bar.symbol:
                continue
            req = self._order_to_request(order)
            if self._would_fill_immediately(req, bar):
                fill_price = self._compute_fill_price(req, bar)
                self._fill_order(broker_order_id, order["quantity"], fill_price)

    def _process_resting_gtts(self, bar: PriceBar) -> None:
        for gtt in self._gtts.values():
            if gtt["status"] != GttStatus.GTT_ACTIVE:
                continue
            if gtt["symbol"] != bar.symbol:
                continue
            if gtt["gtt_type"] == GttType.SINGLE:
                if bar.low <= gtt["trigger_price"] or bar.high >= gtt["trigger_price"]:
                    gtt["status"] = GttStatus.GTT_TRIGGERED
            elif gtt["gtt_type"] == GttType.OCO:
                if bar.low <= gtt["sl_trigger_price"]:
                    gtt["status"] = GttStatus.GTT_TRIGGERED
                    gtt["triggered_leg"] = "sl"
                elif bar.high >= gtt["target_trigger_price"]:
                    gtt["status"] = GttStatus.GTT_TRIGGERED
                    gtt["triggered_leg"] = "target"

    def _emit_ticks(self, bar: PriceBar) -> None:
        for symbols, callback in self._tick_callbacks:
            if bar.symbol in symbols:
                callback(bar.symbol, bar.close)

    def _notify_order_update(self, order: dict) -> None:
        for callback in self._order_callbacks:
            callback(order)

    @staticmethod
    def _order_to_request(order: dict) -> OrderRequest:
        return OrderRequest(
            symbol=order["symbol"],
            exchange=order["exchange"],
            side=order["side"],
            order_type=order["order_type"],
            quantity=order["quantity"],
            product=order["product"],
            price=order["price"],
            trigger_price=order["trigger_price"],
        )
