from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from executor.brokers.mock_broker import MockBroker
from executor.gtt_manager import GttManager
from executor.models import (
    BrokerName,
)
from executor.position_manager import PositionManager

IST = ZoneInfo("Asia/Kolkata")

def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    return db


def _seed_position(db: sqlite3.Connection, **overrides) -> str:
    now = datetime.now(IST).isoformat()
    defaults = dict(
        position_id="pos-1",
        symbol="RELIANCE",
        exchange="NSE",
        track="swing",
        bucket_id="swing_bucket",
        broker_id="fyers",
        quantity=10,
        average_entry_price=2500.0,
        current_price=2500.0,
        unrealised_pnl=0.0,
        realised_pnl=0.0,
        stop_loss_price=2400.0,
        target_price=2700.0,
        atr_at_entry=20.0,
        entry_order_id="entry-1",
        gtt_oco_id=None,
        unprotected_flag=0,
        unmanaged=0,
        health_score=80.0,
        is_open=1,
        entry_at=now,
        exit_at=None,
        trade_plan_id=None,
        recommendation_id=None,
    )
    defaults.update(overrides)
    db.execute(
        """INSERT INTO positions
        (position_id, symbol, exchange, track, bucket_id, broker_id, quantity,
         average_entry_price, current_price, unrealised_pnl, realised_pnl,
         stop_loss_price, target_price, atr_at_entry, entry_order_id, gtt_oco_id,
         unprotected_flag, unmanaged, health_score, is_open, entry_at, exit_at,
         trade_plan_id, recommendation_id)
        VALUES (:position_id,:symbol,:exchange,:track,:bucket_id,:broker_id,:quantity,
                :average_entry_price,:current_price,:unrealised_pnl,:realised_pnl,
                :stop_loss_price,:target_price,:atr_at_entry,:entry_order_id,:gtt_oco_id,
                :unprotected_flag,:unmanaged,:health_score,:is_open,:entry_at,:exit_at,
                :trade_plan_id,:recommendation_id)""",
        defaults,
    )
    db.commit()
    return defaults["position_id"]


class TestPositionManagerOpen:
    def test_open_position_creates_row(self):
        db = _make_db()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: MockBroker()})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())

        pos_id = pm.open_position(
            symbol="TCS",
            exchange="NSE",
            track="long_term",
            bucket_id="lt_bucket",
            broker_id=BrokerName.FYERS,
            quantity=5,
            average_entry_price=3000.0,
            stop_loss_price=2800.0,
            target_price=3500.0,
            atr_at_entry=50.0,
            entry_order_id="order-1",
        )
        row = db.execute("SELECT * FROM positions WHERE position_id=?", (pos_id,)).fetchone()
        assert row["symbol"] == "TCS"
        assert row["is_open"] == 1
        assert row["unprotected_flag"] == 1  # new position starts unprotected

    def test_close_position_marks_closed(self):
        db = _make_db()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: MockBroker()})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())
        _seed_position(db)

        pm.close_position("pos-1", exit_price=2600.0, realised_pnl=1000.0)
        row = db.execute("SELECT * FROM positions WHERE position_id=?", ("pos-1",)).fetchone()
        assert row["is_open"] == 0
        assert row["realised_pnl"] == pytest.approx(1000.0)


class TestPositionManagerLTP:
    def test_update_ltp_updates_unrealised_pnl(self):
        db = _make_db()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: MockBroker()})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())
        _seed_position(db, quantity=10, average_entry_price=2500.0)

        pm.update_ltp("RELIANCE", 2550.0)
        row = db.execute(
            "SELECT unrealised_pnl FROM positions WHERE position_id=?", ("pos-1",)
        ).fetchone()
        # 10 shares × (2550 - 2500) = 500
        assert row["unrealised_pnl"] == pytest.approx(500.0)


class TestPositionManagerUnprotected:
    def test_mark_and_clear_unprotected(self):
        db = _make_db()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: MockBroker()})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())
        _seed_position(db, unprotected_flag=0)

        pm.mark_unprotected("pos-1")
        row = db.execute(
            "SELECT unprotected_flag FROM positions WHERE position_id=?", ("pos-1",)
        ).fetchone()
        assert row["unprotected_flag"] == 1

        pm.clear_unprotected("pos-1")
        row = db.execute(
            "SELECT unprotected_flag FROM positions WHERE position_id=?", ("pos-1",)
        ).fetchone()
        assert row["unprotected_flag"] == 0


class TestPositionManagerGraduation:
    def test_graduation_requires_swing_track(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())
        # Long-term position cannot graduate
        _seed_position(db, track="long_term")
        result = pm.graduate_position("pos-1", current_price=2600.0)
        assert result is False

    def test_graduation_requires_sufficient_gain(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())
        _seed_position(db, track="swing", average_entry_price=2500.0, atr_at_entry=20.0)
        # Only 5 pts gain < 1×ATR
        result = pm.graduate_position("pos-1", current_price=2505.0)
        assert result is False

    def test_graduation_success_updates_track(self):
        db = _make_db()
        broker = MockBroker()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: broker, BrokerName.MOCK: broker})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())
        _seed_position(
            db, track="swing", average_entry_price=2500.0, atr_at_entry=20.0, gtt_oco_id=None
        )

        # Gain of 50 > 1×ATR=20 → graduation proceeds
        result = pm.graduate_position("pos-1", current_price=2550.0)
        assert result is True
        row = db.execute(
            "SELECT track, stop_loss_price FROM positions WHERE position_id=?", ("pos-1",)
        ).fetchone()
        assert row["track"] == "long_term"
        # New stop = 2550 - 3×20 = 2490
        assert row["stop_loss_price"] == pytest.approx(2490.0)


class TestPositionManagerLoadOpen:
    def test_load_open_filters_by_track(self):
        db = _make_db()
        gm = GttManager(db=db, brokers={BrokerName.FYERS: MockBroker()})
        pm = PositionManager(db=db, gtt_manager=gm, order_manager=MagicMock())
        _seed_position(db, position_id="pos-1", track="swing")
        _seed_position(db, position_id="pos-2", track="intraday")

        swing_positions = pm.load_open("swing")
        assert len(swing_positions) == 1
        assert swing_positions[0].track == "swing"
