from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest.mock import MagicMock

from executor.models import (
    BrokerFunds,
    BrokerName,
    BrokerPosition,
    ProductType,
)
from executor.reconciliation import ReconciliationLoop


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    return db


def _seed_position(
    db: sqlite3.Connection, symbol: str, broker_id: str, track: str = "swing"
) -> None:
    now = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO positions
        (position_id, symbol, exchange, track, bucket_id, broker_id, quantity,
         average_entry_price, current_price, unrealised_pnl, realised_pnl,
         stop_loss_price, target_price, atr_at_entry, entry_order_id, gtt_oco_id,
         unprotected_flag, unmanaged, health_score, is_open, entry_at)
        VALUES (?,?,?,?,?,?,10,100,100,0,0,90,120,5,'entry-1',NULL,0,0,80,1,?)""",
        (f"pos-{symbol}", symbol, "NSE", track, "swing_bucket", broker_id, now),
    )
    db.commit()


class TestReconciliationMatchCase:
    def test_no_alerts_when_positions_match(self):
        db = _make_db()
        broker = MagicMock()
        broker.list_positions.return_value = [
            BrokerPosition("RELIANCE", "NSE", 10, 2500.0, 2510.0, ProductType.MIS, BrokerName.KITE)
        ]
        broker.list_holdings.return_value = []
        broker.get_funds.return_value = BrokerFunds(500_000, 0, BrokerName.KITE)

        _seed_position(db, "RELIANCE", "kite", track="intraday")
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: broker})
        alerts = rl.reconcile_intraday()
        assert alerts == []


class TestReconciliationBotOnly:
    def test_bot_only_position_raises_alert(self):
        db = _make_db()
        broker = MagicMock()
        broker.list_positions.return_value = []
        broker.list_holdings.return_value = []
        broker.get_funds.return_value = BrokerFunds(500_000, 0, BrokerName.KITE)

        _seed_position(db, "TCS", "kite", track="intraday")
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: broker})
        alerts = rl.reconcile_intraday()
        assert len(alerts) == 1
        row = db.execute(
            "SELECT alert_type FROM reconciliation_alerts WHERE alert_id=?", (alerts[0],)
        ).fetchone()
        assert row["alert_type"] == "position_bot_only"


class TestReconciliationBrokerOnly:
    def test_broker_only_position_raises_alert(self):
        db = _make_db()
        broker = MagicMock()
        broker.list_positions.return_value = [
            BrokerPosition("INFY", "NSE", 20, 1500.0, 1520.0, ProductType.MIS, BrokerName.KITE)
        ]
        broker.list_holdings.return_value = []
        broker.get_funds.return_value = BrokerFunds(500_000, 0, BrokerName.KITE)

        # No INFY in bot's DB
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: broker})
        alerts = rl.reconcile_intraday()
        assert len(alerts) == 1
        row = db.execute(
            "SELECT alert_type FROM reconciliation_alerts WHERE alert_id=?", (alerts[0],)
        ).fetchone()
        assert row["alert_type"] == "position_broker_only"


class TestReconciliationResolve:
    def test_resolve_alert(self):
        db = _make_db()
        broker = MagicMock()
        broker.list_positions.return_value = []
        broker.list_holdings.return_value = []
        broker.get_funds.return_value = BrokerFunds(500_000, 0, BrokerName.KITE)

        _seed_position(db, "WIPRO", "kite", track="intraday")
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: broker})
        alerts = rl.reconcile_intraday()
        rl.resolve_alert(alerts[0], "manual investigation complete")

        assert not rl.has_open_alerts()
