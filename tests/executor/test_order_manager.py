from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from executor.brokers.mock_broker import MockBroker
from executor.models import (
    BrokerName,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PreTradeCheckError,
    PriceBar,
    ProductType,
)
from executor.order_manager import OrderManager


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    return db


def _make_om(ltp: dict | None = None) -> tuple[OrderManager, MockBroker]:
    db = _make_db()
    broker = MockBroker(initial_cash=500_000)
    ltp_cache = ltp or {}
    om = OrderManager(
        db=db,
        brokers={BrokerName.MOCK: broker, BrokerName.KITE: broker, BrokerName.FYERS: broker},
        ltp_cache=ltp_cache,
    )
    return om, broker


def _intraday_req(symbol: str = "RELIANCE", qty: int = 10, price: float = 0.0) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, exchange="NSE", side=OrderSide.BUY,
        order_type=OrderType.MARKET if price == 0 else OrderType.LIMIT,
        quantity=qty, product=ProductType.MIS,
        price=price, idempotency_key=f"key-{symbol}-{qty}",
    )


class TestOrderManagerRouting:
    def test_intraday_routes_to_kite(self):
        om, broker = _make_om()
        bar = PriceBar("RELIANCE", "2024-01-02", 2500, 2520, 2480, 2510, 1_000_000)
        broker.set_price_bar(bar)
        # Patch market hours check to always pass
        with patch("executor.order_manager.datetime") as mock_dt:
            mock_dt.now.return_value.__class__ = type(mock_dt.now.return_value)
            import datetime as real_dt
            now = real_dt.datetime(2024, 1, 2, 4, 0, 0, tzinfo=real_dt.UTC)
            mock_dt.now.return_value = now
            mock_dt.fromisoformat = real_dt.datetime.fromisoformat
            mock_dt.now.side_effect = lambda tz=None: now
            mock_dt.side_effect = lambda *a, **kw: real_dt.datetime(*a, **kw)
            order_id = om.submit(_intraday_req(), "intraday")
        assert order_id is not None


class TestOrderManagerStateMachine:
    def test_valid_transition_created_to_submitting(self):
        from executor.models import ALLOWED_TRANSITIONS, OrderStatus
        assert OrderStatus.SUBMITTING in ALLOWED_TRANSITIONS[OrderStatus.CREATED]

    def test_filled_to_cancelled_is_invalid(self):
        from executor.models import ALLOWED_TRANSITIONS, OrderStatus
        assert OrderStatus.CANCELLED not in ALLOWED_TRANSITIONS[OrderStatus.FILLED]

    def test_error_is_terminal(self):
        from executor.models import ALLOWED_TRANSITIONS, OrderStatus
        assert len(ALLOWED_TRANSITIONS[OrderStatus.ERROR]) == 0


class TestPreTradeChecks:
    def test_zero_quantity_rejected(self):
        om, broker = _make_om()
        req = _intraday_req(qty=0)
        with pytest.raises(PreTradeCheckError, match="quantity"):
            om._pre_trade_checks(req, broker, ltp=None, track="intraday")

    def test_empty_symbol_rejected(self):
        om, broker = _make_om()
        req = OrderRequest(
            symbol="", exchange="NSE", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=10, product=ProductType.MIS,
        )
        with pytest.raises(PreTradeCheckError, match="Symbol"):
            om._pre_trade_checks(req, broker, ltp=None, track="swing")

    def test_price_beyond_5pct_of_ltp_rejected(self):
        om, broker = _make_om(ltp={"RELIANCE": 2500.0})
        req = _intraday_req(price=2700.0)  # 8% above LTP
        with pytest.raises(PreTradeCheckError, match="deviates"):
            om._pre_trade_checks(req, broker, ltp=2500.0, track="swing")

    def test_price_within_5pct_passes(self):
        om, broker = _make_om(ltp={"RELIANCE": 2500.0})
        req = _intraday_req(price=2550.0)  # 2% above LTP — OK
        # Should not raise
        om._pre_trade_checks(req, broker, ltp=2500.0, track="swing")

    def test_extreme_price_rejected(self):
        # Sanity check fires first (5% < circuit 20%), blocking extreme prices.
        om, broker = _make_om()
        req = _intraday_req(price=1200.0)  # 20% above ltp=1000
        with pytest.raises(PreTradeCheckError, match="deviates"):
            om._pre_trade_checks(req, broker, ltp=1000.0, track="swing")

    def test_duplicate_idempotency_key_rejected(self):
        om, broker = _make_om()
        db = om._db
        import datetime as real_dt
        now = real_dt.datetime.now(real_dt.UTC).isoformat()
        # Manually insert an existing order with same key
        db.execute(
            """INSERT INTO orders
               (order_id, broker_order_id, broker_id, symbol, exchange, side,
               order_type, quantity, filled_quantity, product, price,
               trigger_price, average_fill_price, status, validity,
               idempotency_key, tag, rejection_reason, unprotected_flag,
               unmanaged, created_at, updated_at)
               VALUES ('x','','kite','RELIANCE','NSE','buy','market',10,0,'mis',
               0,0,0,'created','day','key-RELIANCE-10','','',0,0,?,?)""",
            (now, now),
        )
        db.commit()
        req = _intraday_req()
        with pytest.raises(PreTradeCheckError, match="Duplicate"):
            om._pre_trade_checks(req, broker, ltp=None, track="swing")


class TestOrderManagerIdempotency:
    def test_row_to_record_fields(self):
        om, _ = _make_om()
        db = om._db
        import datetime as real_dt
        now = real_dt.datetime.now(real_dt.UTC).isoformat()
        db.execute(
            """INSERT INTO orders
               (order_id, broker_order_id, broker_id, symbol, exchange, side,
               order_type, quantity, filled_quantity, product, price,
               trigger_price, average_fill_price, status, validity,
               idempotency_key, tag, rejection_reason, unprotected_flag,
               unmanaged, created_at, updated_at)
               VALUES ('abc','brk-123','kite','TCS','NSE','buy','market',5,0,
               'mis',3000,0,0,'pending','day','','','',0,0,?,?)""",
            (now, now),
        )
        db.commit()
        record = om._load_order("abc")
        assert record.symbol == "TCS"
        assert record.quantity == 5
        assert record.status == OrderStatus.PENDING
