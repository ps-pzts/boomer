from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from executor.models import (
    BrokerFunds,
    BrokerName,
    BrokerPosition,
    ProductType,
)
from executor.reconciliation import ReconciliationLoop

IST = ZoneInfo("Asia/Kolkata")


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


def _seed_position(
    db: sqlite3.Connection, symbol: str, broker_id: str, track: str = "intraday"
) -> None:
    now = datetime.now(IST).isoformat()
    db.execute(
        """INSERT INTO positions
        (position_id, symbol, exchange, track, bucket_id, broker_id, quantity,
         average_entry_price, current_price, unrealised_pnl, realised_pnl,
         stop_loss_price, target_price, atr_at_entry, entry_order_id, gtt_oco_id,
         unprotected_flag, unmanaged, health_score, is_open, entry_at)
        VALUES (?,?,?,?,?,?,10,100,100,0,0,90,120,5,'entry-1',NULL,0,0,80,1,?)""",
        (f"pos-{symbol}", symbol, "NSE", track, "intraday_bucket", broker_id, now),
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


def _broker(positions=(), holdings=(), cash=500_000.0):
    b = MagicMock()
    b.list_positions.return_value = list(positions)
    b.list_holdings.return_value = list(holdings)
    b.get_funds.return_value = BrokerFunds(cash, 0, BrokerName.KITE)
    return b


def _bp(symbol, qty, price=100.0):
    return BrokerPosition(symbol, "NSE", qty, price, price, ProductType.MIS, BrokerName.KITE)


def _alert_rows(db):
    return [dict(r) for r in db.execute("SELECT * FROM reconciliation_alerts ORDER BY symbol")]


class TestAlertContent:
    def test_quantity_mismatch_records_both_sides(self):
        db = _make_db()
        _seed_position(db, "TCS", "kite")  # bot: 10 shares
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: _broker([_bp("TCS", 7)])})
        assert len(rl.reconcile_intraday()) == 1
        (row,) = _alert_rows(db)
        assert (row["alert_type"], row["symbol"], row["exchange"]) == (
            "quantity_mismatch", "TCS", "NSE",
        )  # fmt: skip
        assert json.loads(row["bot_value"]) == {"quantity": 10}
        assert json.loads(row["broker_value"]) == {"quantity": 7}
        assert row["resolved"] == 0

    def test_bot_only_records_symbol_and_position(self):
        db = _make_db()
        _seed_position(db, "TCS", "kite")
        ReconciliationLoop(db=db, brokers={BrokerName.KITE: _broker()}).reconcile_intraday()
        (row,) = _alert_rows(db)
        assert (row["symbol"], row["exchange"], row["broker_value"]) == ("TCS", "NSE", None)
        assert json.loads(row["bot_value"]) == {"quantity": 10, "position_id": "pos-TCS"}

    def test_broker_only_records_symbol_and_values(self):
        db = _make_db()
        rl = ReconciliationLoop(
            db=db, brokers={BrokerName.KITE: _broker([_bp("INFY", 20, 1500.0)])}
        )
        rl.reconcile_intraday()
        (row,) = _alert_rows(db)
        assert (row["symbol"], row["exchange"], row["bot_value"]) == ("INFY", "NSE", None)
        assert json.loads(row["broker_value"]) == {"quantity": 20, "avg_price": 1500.0}


class TestEod:
    def test_paper_broker_is_skipped(self):
        db = _make_db()
        paper = _broker([_bp("INFY", 5)], cash=-1.0)
        rl = ReconciliationLoop(db=db, brokers={BrokerName.PAPER: paper})
        assert rl.reconcile_eod() == []
        assert rl.reconcile_intraday() == []
        paper.list_positions.assert_not_called()

    def test_holdings_are_compared_with_positions(self):
        db = _make_db()
        rl = ReconciliationLoop(
            db=db, brokers={BrokerName.KITE: _broker([_bp("A", 1)], [_bp("B", 2)])}
        )
        alerts = rl.reconcile_eod()
        assert sorted(r["symbol"] for r in _alert_rows(db)) == ["A", "B"]
        assert len(alerts) == 2
        # the same on the intraday cadence
        db2 = _make_db()
        rl2 = ReconciliationLoop(
            db=db2, brokers={BrokerName.KITE: _broker([_bp("A", 1)], [_bp("B", 2)])}
        )
        assert len(rl2.reconcile_intraday()) == 2

    def test_negative_cash_raises_a_cash_alert(self):
        db = _make_db()
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: _broker(cash=-250.0)})
        alerts = rl.reconcile_eod()
        assert len(alerts) == 1
        (row,) = _alert_rows(db)
        assert row["alert_type"] == "eod_cash_mismatch"
        assert (row["symbol"], row["exchange"], row["bot_value"]) == (None, None, None)
        assert json.loads(row["broker_value"]) == {"available_cash": -250.0}

    @pytest.mark.parametrize("cash", [0.0, 0.5, 1.0, 500_000.0])
    def test_zero_or_positive_cash_is_fine(self, cash, caplog):
        db = _make_db()
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: _broker(cash=cash)})
        with caplog.at_level("ERROR"):
            assert rl.reconcile_eod() == []
        assert caplog.records == []  # and nothing blew up inside

    def test_funds_unavailable_is_not_an_alert(self, caplog):
        db = _make_db()
        b = _broker()
        b.get_funds.side_effect = RuntimeError("kite down")
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: b})
        with caplog.at_level("ERROR"):
            assert rl.reconcile_eod() == []
        assert caplog.records == []

    def test_one_broker_failing_does_not_stop_the_report(self, caplog):
        db = _make_db()
        bad = MagicMock()
        bad.list_positions.side_effect = RuntimeError("boom")
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: bad})
        with caplog.at_level("ERROR"):
            assert rl.reconcile_eod() == []
            assert rl.reconcile_intraday() == []
        assert len(caplog.records) == 2


class TestOpenAlerts:
    def test_has_open_alerts_tracks_unresolved_rows(self):
        db = _make_db()
        rl = ReconciliationLoop(db=db, brokers={BrokerName.KITE: _broker([_bp("INFY", 1)])})
        assert not rl.has_open_alerts()
        (alert,) = rl.reconcile_intraday()
        assert rl.has_open_alerts()  # exactly one open alert is enough
        rl.resolve_alert(alert, "checked")
        (row,) = _alert_rows(db)
        assert (row["resolved"], row["resolution_note"]) == (1, "checked")
        assert row["resolved_at"]


class TestRowToPosition:
    def _row(self, **extra):
        base = {
            "position_id": "p1", "symbol": "TCS", "exchange": "NSE", "track": "intraday",
            "bucket_id": "b", "broker_id": "kite", "quantity": 3, "average_entry_price": 100.0,
            "stop_loss_price": 90.0, "target_price": 120.0, "atr_at_entry": 5.0,
            "entry_order_id": "e1", "entry_at": "2026-11-19T10:00:00",
        }  # fmt: skip
        return ReconciliationLoop._row_to_position({**base, **extra})

    def test_missing_optional_columns_take_safe_defaults(self):
        p = self._row()
        assert (p.current_price, p.unrealised_pnl, p.realised_pnl) == (0.0, 0.0, 0.0)
        assert (p.unprotected_flag, p.unmanaged, p.is_open) == (False, False, True)
        assert p.health_score == 100.0
        assert (p.unprotected_since, p.exit_at, p.gtt_oco_id) == (None, None, None)

    def test_present_columns_are_kept(self):
        p = self._row(
            current_price=105.0, unrealised_pnl=15.0, realised_pnl=2.0, unprotected_flag=1,
            unmanaged=1, health_score=40.0, is_open=0, unprotected_since="2026-11-19T10:05:00",
            exit_at="2026-11-19T11:00:00",
        )  # fmt: skip
        assert (p.current_price, p.unrealised_pnl, p.realised_pnl) == (105.0, 15.0, 2.0)
        assert (p.unprotected_flag, p.unmanaged, p.is_open) == (True, True, False)
        assert p.health_score == 40.0
        assert p.unprotected_since == datetime(2026, 11, 19, 10, 5)
        assert p.exit_at == datetime(2026, 11, 19, 11, 0)
