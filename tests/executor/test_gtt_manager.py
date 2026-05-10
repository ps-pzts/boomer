from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from executor.brokers.mock_broker import MockBroker
from executor.gtt_manager import GttManager
from executor.models import (
    BrokerName,
    GttRequest,
    GttStatus,
    GttType,
    PositionRecord,
)


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    return db


def _make_position(
    symbol: str = "RELIANCE", atr: float = 20.0, sl: float = 2400.0
) -> PositionRecord:
    now = datetime.now(UTC)
    return PositionRecord(
        position_id="pos-1",
        symbol=symbol, exchange="NSE", track="swing",
        bucket_id="swing_bucket", broker_id=BrokerName.FYERS,
        quantity=10, average_entry_price=2500.0,
        current_price=2500.0, unrealised_pnl=0.0, realised_pnl=0.0,
        stop_loss_price=sl, target_price=2700.0, atr_at_entry=atr,
        entry_order_id="entry-order-1", gtt_oco_id=None,
        unprotected_flag=True, unprotected_since=now, unmanaged=False,
        health_score=80.0, is_open=True, entry_at=now, exit_at=None,
        trade_plan_id=None, recommendation_id=None,
    )


class TestGttManagerPlacement:
    def test_place_single_gtt_creates_record(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pos = _make_position()
        req = GttRequest(
            symbol="RELIANCE", exchange="NSE", gtt_type=GttType.SINGLE,
            quantity=10, trigger_price=2400.0, limit_price=2395.0,
        )
        gtt_id = gm.place_gtt_for_position(pos, GttType.SINGLE, req)
        row = db.execute("SELECT * FROM gtt_orders WHERE gtt_id=?", (gtt_id,)).fetchone()
        assert row is not None
        assert row["status"] == GttStatus.GTT_ACTIVE
        assert row["trigger_price"] == 2400.0

    def test_place_oco_gtt_creates_record(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pos = _make_position()
        req = GttRequest(
            symbol="RELIANCE", exchange="NSE", gtt_type=GttType.OCO,
            quantity=10,
            sl_trigger_price=2400.0, sl_limit_price=2395.0,
            target_trigger_price=2700.0, target_limit_price=2705.0,
            parent_order_id="entry-order-1",
        )
        gtt_id = gm.place_gtt_for_position(pos, GttType.OCO, req)
        row = db.execute("SELECT * FROM gtt_orders WHERE gtt_id=?", (gtt_id,)).fetchone()
        assert row["gtt_type"] == GttType.OCO
        assert row["sl_trigger_price"] == 2400.0
        assert row["target_trigger_price"] == 2700.0

    def test_duplicate_gtt_raises(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pos = _make_position()
        req = GttRequest(
            symbol="RELIANCE", exchange="NSE", gtt_type=GttType.SINGLE,
            quantity=10, trigger_price=2400.0, limit_price=2395.0,
        )
        gm.place_gtt_for_position(pos, GttType.SINGLE, req)
        with pytest.raises(ValueError, match="Duplicate"):
            gm.place_gtt_for_position(pos, GttType.SINGLE, req)


class TestGttManagerTrailingStop:
    def test_trail_stop_moves_up_when_price_gains_2atr(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pos = _make_position(atr=20.0)

        # Place OCO GTT
        req = GttRequest(
            symbol="RELIANCE", exchange="NSE", gtt_type=GttType.OCO,
            quantity=10, sl_trigger_price=2400.0, sl_limit_price=2395.0,
            target_trigger_price=2700.0, target_limit_price=2705.0,
            parent_order_id="entry-order-1",
        )
        gtt_id = gm.place_gtt_for_position(pos, GttType.OCO, req)
        pos.gtt_oco_id = gtt_id

        # Current price = 2500 + 2×20 = 2540 — should trigger trailing
        result = gm.trail_stop(pos, current_price=2542.0)
        assert result is True
        row = db.execute(
            "SELECT sl_trigger_price FROM gtt_orders WHERE gtt_id=?", (gtt_id,)
        ).fetchone()
        # New SL = 2400 + 1×20 = 2420
        assert row["sl_trigger_price"] == pytest.approx(2420.0)

    def test_trail_stop_does_not_move_when_gain_less_than_2atr(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pos = _make_position(atr=20.0)

        req = GttRequest(
            symbol="RELIANCE", exchange="NSE", gtt_type=GttType.OCO,
            quantity=10, sl_trigger_price=2400.0, sl_limit_price=2395.0,
            target_trigger_price=2700.0, target_limit_price=2705.0,
            parent_order_id="entry-order-1",
        )
        gtt_id = gm.place_gtt_for_position(pos, GttType.OCO, req)
        pos.gtt_oco_id = gtt_id

        result = gm.trail_stop(pos, current_price=2530.0)  # only 1.5 ATR gain
        assert result is False
        row = db.execute(
            "SELECT sl_trigger_price FROM gtt_orders WHERE gtt_id=?", (gtt_id,)
        ).fetchone()
        assert row["sl_trigger_price"] == pytest.approx(2400.0)  # unchanged


class TestGttManagerDailyReconcile:
    def test_reconcile_marks_triggered_gtt(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})

        # Place a GTT and then simulate it triggering via the broker
        from executor.models import PriceBar
        req = GttRequest(
            symbol="SAIL", exchange="NSE", gtt_type=GttType.SINGLE,
            quantity=10, trigger_price=90.0, limit_price=89.0,
        )
        pos = _make_position(symbol="SAIL")
        pos.broker_id = BrokerName.MOCK
        gtt_id = gm.place_gtt_for_position(pos, GttType.SINGLE, req)

        # Simulate GTT triggering via bar
        bar = PriceBar("SAIL", "2024-01-02", 92, 93, 88, 91, 500_000)
        broker.set_price_bar(bar)

        triggered = gm.daily_reconcile(BrokerName.MOCK)
        assert gtt_id in triggered
