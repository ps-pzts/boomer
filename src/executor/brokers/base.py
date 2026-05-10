from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from executor.models import (
    BrokerFunds,
    BrokerName,
    BrokerPosition,
    GttRequest,
    OrderRequest,
    PriceBar,
)


class Broker(ABC):
    """
    Abstract broker interface.

    The rest of the executor uses ONLY this interface.
    No code outside executor/brokers/ imports or calls a broker SDK directly.
    Routing (Kite vs Fyers vs Mock vs Paper) is in OrderManager — not here.
    """

    @property
    @abstractmethod
    def broker_id(self) -> BrokerName: ...

    @abstractmethod
    def authenticate(self) -> None:
        """Establish or refresh broker session. Called at 09:00 IST daily."""

    @abstractmethod
    def place_order(self, request: OrderRequest) -> str:
        """Submit order. Returns broker_order_id."""

    @abstractmethod
    def modify_order(self, broker_order_id: str, changes: dict) -> str:
        """Modify a pending order. Returns updated broker_order_id."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel a pending order."""

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> dict:
        """
        Return raw broker order dict. OrderManager normalises it.
        Required keys: status, filled_quantity, average_fill_price, rejection_reason.
        """

    @abstractmethod
    def list_positions(self) -> list[BrokerPosition]:
        """Return all open intraday (MIS) positions."""

    @abstractmethod
    def list_holdings(self) -> list[BrokerPosition]:
        """Return all delivery (CNC) holdings."""

    @abstractmethod
    def get_funds(self) -> BrokerFunds:
        """Return available cash and used margin."""

    @abstractmethod
    def on_order_update(self, callback: Callable[[dict], None]) -> None:
        """Subscribe to broker push events for order state changes."""

    @abstractmethod
    def on_tick(self, symbols: list[str], callback: Callable[[str, float], None]) -> None:
        """Subscribe to price tick feed. callback(symbol, ltp)."""

    # ── GTT methods (delivery/swing only) ────────────────────────────────────

    @abstractmethod
    def place_gtt(self, request: GttRequest) -> str:
        """Place GTT single-leg or OCO. Returns broker_gtt_id."""

    @abstractmethod
    def modify_gtt(self, broker_gtt_id: str, changes: dict) -> None:
        """Modify an active GTT (e.g., trail stop upward)."""

    @abstractmethod
    def cancel_gtt(self, broker_gtt_id: str) -> None:
        """Cancel an active GTT."""

    @abstractmethod
    def get_gtt(self, broker_gtt_id: str) -> dict:
        """Return raw broker GTT dict."""

    @abstractmethod
    def list_gtts(self) -> list[dict]:
        """Return all active GTTs as raw broker dicts."""

    # ── Historical data (used by morning batch and backtester) ───────────────

    def get_historical_ohlcv(
        self,
        symbol: str,
        exchange: str,
        from_date: str,
        to_date: str,
        interval: str = "day",
    ) -> list[PriceBar]:
        """
        Fetch historical OHLCV bars. Optional — only KiteBroker and MockBroker implement.
        Raises NotImplementedError for brokers that don't support it.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support historical data")

    def get_ltp(self, symbol: str, exchange: str) -> float | None:
        """
        Get last traded price via REST (not tick feed).
        Used as fallback when tick is stale (> 5 minutes). See Q4-1.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support LTP lookup")
