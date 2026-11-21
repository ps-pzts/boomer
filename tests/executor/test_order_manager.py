from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from executor.brokers.kite_broker import KiteBroker
from executor.brokers.kitesim import KiteSim
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
from tests.kitesim_kit import live_shaped_broker, make_sim, paper_broker


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0012_events.sql") as f:  # order transitions emit events (#56)
        db.executescript(f.read())
    return db


def _make_om(
    ltp: dict | None = None, cash: float = 500_000
) -> tuple[OrderManager, KiteBroker, KiteSim]:
    """OrderManager on the paper KiteBroker + KiteSim (#86)."""
    db = _make_db()
    sim = make_sim(cash=cash)
    broker = paper_broker(sim)
    ltp_cache = ltp or {}
    om = OrderManager(db=db, brokers={BrokerName.KITE: broker}, ltp_cache=ltp_cache)
    return om, broker, sim


def _intraday_req(symbol: str = "RELIANCE", qty: int = 10, price: float = 0.0) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        exchange="NSE",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET if price == 0 else OrderType.LIMIT,
        quantity=qty,
        product=ProductType.MIS,
        price=price,
        idempotency_key=f"key-{symbol}-{qty}",
    )


class TestOrderManagerRouting:
    def test_intraday_routes_to_kite(self):
        om, broker, sim = _make_om()
        bar = PriceBar("RELIANCE", "2024-01-02", 2500, 2520, 2480, 2510, 1_000_000)
        sim.on_bar(bar)
        # Patch market hours check to always pass
        with patch("executor.order_manager.datetime") as mock_dt:
            mock_dt.now.return_value.__class__ = type(mock_dt.now.return_value)
            import datetime as real_dt

            tz_ist = real_dt.timezone(real_dt.timedelta(hours=5, minutes=30))
            now = real_dt.datetime(2024, 1, 2, 9, 30, 0, tzinfo=tz_ist)
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
    def test_cnc_product_rejected(self):
        # Regression: gtt_dispatch sent intraday recs as CNC delivery orders.
        # Only MIS is allowed, whatever the track.
        om, broker, sim = _make_om()
        req = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10,
            product=ProductType.CNC,
        )
        with pytest.raises(PreTradeCheckError, match="only MIS"):
            om._pre_trade_checks(req, broker, ltp=None, track="intraday")

    def test_cnc_rejected_on_submit_and_never_reaches_broker(self):
        om, broker, sim = _make_om()
        req = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10,
            product=ProductType.CNC,
        )
        with patch.object(broker, "place_order") as place:
            with pytest.raises(PreTradeCheckError):
                om.submit(req, "intraday")
            place.assert_not_called()
        assert om._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

    def test_mis_product_passes_product_check(self):
        om, broker, sim = _make_om()
        req = _intraday_req(qty=10)
        # track="other" skips the market-hours check, so only the product check matters
        om._pre_trade_checks(req, broker, ltp=None, track="other")

    def test_zero_quantity_rejected(self):
        om, broker, sim = _make_om()
        req = _intraday_req(qty=0)
        with pytest.raises(PreTradeCheckError, match="quantity"):
            om._pre_trade_checks(req, broker, ltp=None, track="intraday")

    def test_empty_symbol_rejected(self):
        om, broker, sim = _make_om()
        req = OrderRequest(
            symbol="",
            exchange="NSE",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=10,
            product=ProductType.MIS,
        )
        with pytest.raises(PreTradeCheckError, match="Symbol"):
            om._pre_trade_checks(req, broker, ltp=None, track="other")

    def test_price_beyond_5pct_of_ltp_rejected(self):
        om, broker, sim = _make_om(ltp={"RELIANCE": 2500.0})
        req = _intraday_req(price=2700.0)  # 8% above LTP
        with pytest.raises(PreTradeCheckError, match="deviates"):
            om._pre_trade_checks(req, broker, ltp=2500.0, track="other")

    def test_price_within_5pct_passes(self):
        om, broker, sim = _make_om(ltp={"RELIANCE": 2500.0})
        req = _intraday_req(price=2550.0)  # 2% above LTP — OK
        # Should not raise
        om._pre_trade_checks(req, broker, ltp=2500.0, track="other")

    def test_extreme_price_rejected(self):
        # Sanity check fires first (5% < circuit 20%), blocking extreme prices.
        om, broker, sim = _make_om()
        req = _intraday_req(price=1200.0)  # 20% above ltp=1000
        with pytest.raises(PreTradeCheckError, match="deviates"):
            om._pre_trade_checks(req, broker, ltp=1000.0, track="other")

    def test_duplicate_idempotency_key_rejected(self):
        om, broker, sim = _make_om()
        db = om._db
        import datetime as real_dt

        _tz_ist = real_dt.timezone(real_dt.timedelta(hours=5, minutes=30))
        now = real_dt.datetime.now(_tz_ist).replace(tzinfo=None).isoformat()
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
            om._pre_trade_checks(req, broker, ltp=None, track="other")


class TestOrderManagerIdempotency:
    def test_row_to_record_fields(self):
        om, _, _ = _make_om()
        db = om._db
        import datetime as real_dt

        _tz_ist = real_dt.timezone(real_dt.timedelta(hours=5, minutes=30))
        now = real_dt.datetime.now(_tz_ist).replace(tzinfo=None).isoformat()
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


class TestBrokerResolution:
    """Regression #41: orders placed via the paper broker in the KITE slot recorded
    broker_id='paper', and sync/cancel raised KeyError looking that key up."""

    def _paper_om(self):
        paper = paper_broker(make_sim())
        return OrderManager(db=_make_db(), brokers={BrokerName.KITE: paper}, ltp_cache={}), paper

    def test_sync_status_finds_paper_broker_in_kite_slot(self):
        om, _ = self._paper_om()
        oid = om.submit(_intraday_req(price=100.0), track="other")
        assert om.sync_status(oid).status == OrderStatus.PENDING

    def test_cancel_finds_paper_broker_in_kite_slot(self):
        om, _ = self._paper_om()
        oid = om.submit(_intraday_req(price=100.0), track="other")
        om.cancel(oid)
        assert om.sync_status(oid).status == OrderStatus.CANCELLED

    def test_unknown_broker_raises_clear_error(self):
        om, _ = self._paper_om()
        oid = om.submit(_intraday_req(price=100.0), track="other")
        om._brokers = {BrokerName.KITE: live_shaped_broker(make_sim())}  # labelled kite
        with pytest.raises(RuntimeError, match="broker_id=paper"):
            om.sync_status(oid)


class TestFundsAndClock:
    def test_sell_exit_not_blocked_by_funds(self):
        """Regression (#45): a target LIMIT sell of qty x price failed 'Insufficient
        funds' after the entry had used the cash, blocking exits."""
        om, broker, sim = _make_om(cash=0.0)
        req = OrderRequest(
            symbol="RELIANCE",
            exchange="NSE",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=10,
            product=ProductType.MIS,
            price=2600.0,
        )
        om._pre_trade_checks(req, broker, ltp=None, track="other")  # must not raise

    def test_buy_still_needs_funds(self):
        om, broker, sim = _make_om(cash=0.0)
        with pytest.raises(PreTradeCheckError, match="Insufficient funds"):
            om._pre_trade_checks(_intraday_req(price=2500.0), broker, ltp=None, track="other")

    def test_market_hours_use_injected_clock(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        om, broker, sim = _make_om()
        om._clock = lambda: datetime(2026, 11, 19, 10, 0, tzinfo=ist)
        om._pre_trade_checks(_intraday_req(), broker, ltp=None, track="intraday")
        om._clock = lambda: datetime(2026, 11, 19, 15, 31, tzinfo=ist)
        with pytest.raises(PreTradeCheckError, match="market hours"):
            om._pre_trade_checks(_intraday_req(), broker, ltp=None, track="intraday")


class TestCancelRace:
    """Regression (#86): the broker's cancel postback reaches handle_broker_update
    before cancel_order returns; cancel() then moved cancelled -> cancelled and
    raised StateMachineError (and could record a partial fill twice)."""

    def _om(self, **config):
        sim = make_sim(**config)
        broker = paper_broker(sim)
        om = OrderManager(db=_make_db(), brokers={BrokerName.KITE: broker}, ltp_cache={})
        broker.on_order_update(om.handle_broker_update)
        sim.on_price("RELIANCE", "NSE", 2500.0)
        return om

    def test_cancel_with_postbacks_wired(self):
        om = self._om()
        oid = om.submit(_intraday_req(price=2400.0), track="other")
        assert om.cancel(oid).status == OrderStatus.CANCELLED

    def test_partial_fill_recorded_once(self):
        om = self._om(liquidity_per_tick=3)
        oid = om.submit(_intraday_req(price=2500.0), track="other")  # 3 of 10 fill at once
        rec = om.cancel(oid)
        assert (rec.status, rec.filled_quantity) == (OrderStatus.CANCELLED, 3)
        n = om._db.execute("SELECT SUM(quantity) FROM executions WHERE order_id=?", (oid,))
        assert n.fetchone()[0] == 3


class TestPreTradeBoundaries:
    """Each pre-trade rule at its exact edge (#89). Hand numbers: LTP 100 -> the 5%
    sanity band is 95..105 and the 20% circuit band 80..120; 10 x 2,500 needs 25,000."""

    @staticmethod
    def _req(kind=OrderType.LIMIT, price=100.0, qty=1, side=OrderSide.BUY):
        return OrderRequest(
            "RELIANCE", "NSE", side, kind, qty, ProductType.MIS, price, idempotency_key=""
        )

    def _check(self, req, *, ltp=None, cash=500_000.0, track="other"):
        om, broker, _ = _make_om(cash=cash)
        om._pre_trade_checks(req, broker, ltp=ltp, track=track)

    def test_one_share_is_a_valid_quantity(self):
        self._check(self._req(qty=1))
        with pytest.raises(PreTradeCheckError, match="Invalid quantity: 0"):
            self._check(self._req(qty=0))
        with pytest.raises(PreTradeCheckError, match="Invalid quantity: -1"):
            self._check(self._req(qty=-1))

    def test_price_sanity_band_is_inclusive_at_5pct(self):
        self._check(self._req(price=105.0), ltp=100.0)  # exactly +5%
        self._check(self._req(price=95.0), ltp=100.0)  # exactly -5%
        with pytest.raises(PreTradeCheckError, match="deviates 5.1% from LTP"):
            self._check(self._req(price=105.1), ltp=100.0)
        with pytest.raises(PreTradeCheckError, match="deviates"):
            self._check(self._req(price=94.9), ltp=100.0)

    def test_small_price_is_still_sanity_checked(self):
        with pytest.raises(PreTradeCheckError, match="deviates"):
            self._check(self._req(price=0.5), ltp=100.0)

    def test_priceless_limit_skips_the_price_checks(self):
        self._check(self._req(price=0.0), ltp=100.0)

    def test_market_order_is_not_sanity_checked_but_is_circuit_checked(self):
        m = OrderType.MARKET
        self._check(self._req(kind=m, price=119.0), ltp=100.0)
        with pytest.raises(PreTradeCheckError, match="20% circuit band"):
            self._check(self._req(kind=m, price=120.0), ltp=100.0)  # exactly 20%: locked
        with pytest.raises(PreTradeCheckError, match="20% circuit band"):
            self._check(self._req(kind=m, price=150.0), ltp=100.0)
        with pytest.raises(PreTradeCheckError, match="20% circuit band"):
            self._check(self._req(kind=m, price=0.5), ltp=100.0)  # a small price still counts

    def test_funds_must_cover_price_times_quantity(self):
        req = self._req(price=2500.0, qty=10)  # needs 25,000
        self._check(req, cash=25_000.0)  # exactly enough
        with pytest.raises(PreTradeCheckError, match="need 25000, have 24999"):
            self._check(req, cash=24_999.0)

    def test_small_buy_with_no_cash_is_refused(self):
        with pytest.raises(PreTradeCheckError, match="Insufficient funds"):
            self._check(self._req(price=0.5, qty=1), cash=0.0)

    def test_market_buy_without_a_price_is_not_a_funds_question(self):
        m = self._req(kind=OrderType.MARKET, price=0.0)
        self._check(m, cash=0.0)
        self._check(m, cash=-1.0)

    @pytest.mark.parametrize(
        "h, m, s, us, ok",
        [
            (9, 14, 59, 0, False),
            (9, 15, 0, 0, True),  # the open itself
            (9, 15, 0, 1, True),
            (15, 30, 0, 0, True),  # the close itself
            (15, 30, 0, 1, False),
            (15, 30, 1, 0, False),
        ],
    )
    def test_market_hours_edges(self, h, m, s, us, ok):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        om, broker, _ = _make_om()
        om._clock = lambda: datetime(2026, 11, 19, h, m, s, us, tzinfo=ZoneInfo("Asia/Kolkata"))
        if ok:
            om._pre_trade_checks(self._req(), broker, ltp=None, track="intraday")
        else:
            with pytest.raises(PreTradeCheckError, match="outside market hours"):
                om._pre_trade_checks(self._req(), broker, ltp=None, track="intraday")
