from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "created"
    SUBMITTING = "submitting"
    PENDING = "pending"
    TRIGGERED = "triggered"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ERROR = "error"


# Valid state transitions enforced by OrderManager
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.SUBMITTING, OrderStatus.ERROR}),
    OrderStatus.SUBMITTING: frozenset(
        {OrderStatus.PENDING, OrderStatus.REJECTED, OrderStatus.ERROR}
    ),
    OrderStatus.PENDING: frozenset({
        OrderStatus.TRIGGERED, OrderStatus.PARTIAL, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
        OrderStatus.ERROR,
    }),
    OrderStatus.TRIGGERED: frozenset(
        {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.ERROR}
    ),
    OrderStatus.PARTIAL: frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.ERROR}),
    OrderStatus.FILLED: frozenset({OrderStatus.ERROR}),
    OrderStatus.CANCELLED: frozenset({OrderStatus.ERROR}),
    OrderStatus.REJECTED: frozenset({OrderStatus.ERROR}),
    OrderStatus.EXPIRED: frozenset({OrderStatus.ERROR}),
    OrderStatus.ERROR: frozenset(),
}

TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.ERROR,
})


class GttStatus(StrEnum):
    GTT_ACTIVE = "gtt_active"
    GTT_TRIGGERED = "gtt_triggered"
    GTT_CANCELLED = "gtt_cancelled"
    GTT_EXPIRED = "gtt_expired"
    GTT_DELETED = "gtt_deleted"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    SL = "sl"           # stop-loss market
    SL_LIMIT = "sl_limit"


class OrderValidity(StrEnum):
    DAY = "day"
    IOC = "ioc"


class ProductType(StrEnum):
    MIS = "mis"   # intraday (Kite)
    CNC = "cnc"   # delivery (Fyers)


class GttType(StrEnum):
    SINGLE = "single"
    OCO = "oco"


class BrokerName(StrEnum):
    KITE = "kite"
    FYERS = "fyers"
    MOCK = "mock"
    PAPER = "paper"


class ReconciliationAlertType(StrEnum):
    POSITION_BOT_ONLY = "position_bot_only"
    POSITION_BROKER_ONLY = "position_broker_only"
    GTT_MISSING = "gtt_missing"
    QUANTITY_MISMATCH = "quantity_mismatch"
    PRICE_MISMATCH = "price_mismatch"
    EOD_CASH_MISMATCH = "eod_cash_mismatch"


class ExecutorErrorType(StrEnum):
    API_TIMEOUT = "api_timeout"
    AUTH_FAILURE = "auth_failure"
    RATE_LIMIT = "rate_limit"
    ORDER_REJECTED = "order_rejected"
    GTT_REJECTED = "gtt_rejected"
    CONNECTION_LOST = "connection_lost"
    STATE_MACHINE_VIOLATION = "state_machine_violation"
    UNKNOWN = "unknown"


@dataclass
class OrderRequest:
    symbol: str
    exchange: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    product: ProductType
    price: float = 0.0
    trigger_price: float = 0.0
    validity: OrderValidity = OrderValidity.DAY
    idempotency_key: str = ""
    tag: str = ""
    parent_order_id: str | None = None
    trade_plan_id: str | None = None
    recommendation_id: str | None = None


@dataclass
class GttRequest:
    symbol: str
    exchange: str
    gtt_type: GttType
    quantity: int
    # single-leg fields
    trigger_price: float = 0.0
    limit_price: float = 0.0
    # OCO fields
    sl_trigger_price: float = 0.0
    sl_limit_price: float = 0.0
    target_trigger_price: float = 0.0
    target_limit_price: float = 0.0
    parent_order_id: str | None = None
    valid_days: int = 365


@dataclass
class OrderRecord:
    order_id: str
    broker_order_id: str
    broker_id: BrokerName
    symbol: str
    exchange: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    filled_quantity: int
    product: ProductType
    price: float
    trigger_price: float
    average_fill_price: float
    status: OrderStatus
    validity: OrderValidity
    idempotency_key: str
    tag: str
    rejection_reason: str
    parent_order_id: str | None
    parent_gtt_id: str | None
    trade_plan_id: str | None
    recommendation_id: str | None
    unprotected_flag: bool
    unmanaged: bool
    created_at: datetime
    updated_at: datetime


@dataclass
class ExecutionRecord:
    execution_id: str
    order_id: str
    broker_execution_id: str
    quantity: int
    price: float
    side: OrderSide
    executed_at: datetime


@dataclass
class PositionRecord:
    position_id: str
    symbol: str
    exchange: str
    track: str            # intraday | swing | long_term
    bucket_id: str
    broker_id: BrokerName
    quantity: int
    average_entry_price: float
    current_price: float
    unrealised_pnl: float
    realised_pnl: float
    stop_loss_price: float
    target_price: float
    atr_at_entry: float
    entry_order_id: str
    gtt_oco_id: str | None
    unprotected_flag: bool
    unprotected_since: datetime | None
    unmanaged: bool
    health_score: float
    is_open: bool
    entry_at: datetime
    exit_at: datetime | None
    trade_plan_id: str | None
    recommendation_id: str | None

    @property
    def unrealised_pnl_pct(self) -> float:
        if self.average_entry_price == 0:
            return 0.0
        return self.unrealised_pnl / (self.average_entry_price * self.quantity)


@dataclass
class GttOrderRecord:
    gtt_id: str
    broker_gtt_id: str
    broker_id: BrokerName
    symbol: str
    exchange: str
    gtt_type: GttType
    trigger_price: float
    limit_price: float
    sl_trigger_price: float
    sl_limit_price: float
    target_trigger_price: float
    target_limit_price: float
    quantity: int
    status: GttStatus
    parent_order_id: str | None
    triggered_order_id: str | None
    valid_until: datetime
    created_at: datetime
    last_checked_at: datetime


@dataclass
class ReconciliationAlert:
    alert_id: str
    broker_id: BrokerName
    alert_type: ReconciliationAlertType
    symbol: str | None
    exchange: str | None
    bot_value: str | None    # JSON
    broker_value: str | None # JSON
    resolved: bool
    resolved_at: datetime | None
    resolution_note: str | None
    created_at: datetime


@dataclass
class BrokerPosition:
    """Normalised position as returned by list_positions() / list_holdings()."""
    symbol: str
    exchange: str
    quantity: int
    average_price: float
    last_price: float
    product: ProductType
    broker_id: BrokerName


@dataclass
class BrokerFunds:
    available_cash: float
    used_margin: float
    broker_id: BrokerName


@dataclass
class PriceBar:
    """Single OHLCV bar for backtesting and slippage simulation."""
    symbol: str
    date: str       # ISO date
    open: float
    high: float
    low: float
    close: float
    volume: int


class StateMachineError(Exception):
    """Raised when an invalid order state transition is attempted."""


class PreTradeCheckError(Exception):
    """Raised when an order fails a pre-trade safety check."""
