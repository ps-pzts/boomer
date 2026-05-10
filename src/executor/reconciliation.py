from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime

from executor.brokers.base import Broker
from executor.models import (
    BrokerName,
    BrokerPosition,
    PositionRecord,
    ReconciliationAlertType,
)

logger = logging.getLogger(__name__)


class ReconciliationLoop:
    """
    Keeps the bot's internal view of positions consistent with broker reality.

    Three cadences (per design doc):
    - Intraday (60s): compare live positions for both Kite (MIS) and Fyers (CNC today)
    - Daily 6 AM: GTT status sync (handled by GttManager.daily_reconcile)
    - EOD: full position + cash reconciliation across both brokers

    Two brokers double the drift surface area — each reconciliation covers both.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        brokers: dict[BrokerName, Broker],
    ) -> None:
        self._db = db
        self._brokers = brokers

    # ── 60-second intraday reconciliation ────────────────────────────────────

    def reconcile_intraday(self) -> list[str]:
        """
        Compare bot's open positions against both broker live views.
        Returns list of alert_ids raised.
        """
        alerts: list[str] = []
        for broker_id, broker in self._brokers.items():
            if broker_id in (BrokerName.MOCK, BrokerName.PAPER):
                continue
            try:
                if broker_id == BrokerName.KITE:
                    broker_positions = broker.list_positions()
                else:
                    broker_positions = broker.list_holdings()
                alerts.extend(self._compare(broker_id, broker_positions, track_filter=None))
            except Exception as exc:
                logger.error("Reconciliation failed for %s: %s", broker_id, exc)
        return alerts

    # ── EOD full reconciliation ───────────────────────────────────────────────

    def reconcile_eod(self) -> list[str]:
        """
        Full cross-broker reconciliation: positions, holdings, and cash.
        Returns list of alert_ids raised. Tomorrow's run is blocked until these clear.
        """
        alerts: list[str] = []
        for broker_id, broker in self._brokers.items():
            if broker_id in (BrokerName.MOCK, BrokerName.PAPER):
                continue
            try:
                broker_positions = broker.list_positions() + broker.list_holdings()
                alerts.extend(self._compare(broker_id, broker_positions, track_filter=None))
                alerts.extend(self._reconcile_cash(broker_id, broker))
            except Exception as exc:
                logger.error("EOD reconciliation failed for %s: %s", broker_id, exc)
        return alerts

    def has_open_alerts(self) -> bool:
        row = self._db.execute(
            "SELECT COUNT(*) FROM reconciliation_alerts WHERE resolved=0"
        ).fetchone()
        return (row[0] if row else 0) > 0

    def resolve_alert(self, alert_id: str, note: str) -> None:
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            "UPDATE reconciliation_alerts"
            " SET resolved=1, resolved_at=?, resolution_note=? WHERE alert_id=?",
            (now, note, alert_id),
        )
        self._db.commit()

    # ── Private ───────────────────────────────────────────────────────────────

    def _compare(
        self,
        broker_id: BrokerName,
        broker_positions: list[BrokerPosition],
        track_filter: str | None,
    ) -> list[str]:
        bot_positions = self._load_bot_positions(broker_id, track_filter)
        bot_by_symbol = {(p.symbol, p.exchange): p for p in bot_positions}
        broker_by_symbol = {(p.symbol, p.exchange): p for p in broker_positions}

        alerts: list[str] = []

        for key, bot_pos in bot_by_symbol.items():
            if key not in broker_by_symbol:
                alerts.append(
                    self._raise_alert(
                        broker_id,
                        ReconciliationAlertType.POSITION_BOT_ONLY,
                        symbol=key[0],
                        exchange=key[1],
                        bot_value=json.dumps(
                            {
                                "quantity": bot_pos.quantity,
                                "position_id": bot_pos.position_id,
                            }
                        ),
                        broker_value=None,
                    )
                )
            else:
                broker_pos = broker_by_symbol[key]
                if bot_pos.quantity != broker_pos.quantity:
                    alerts.append(
                        self._raise_alert(
                            broker_id,
                            ReconciliationAlertType.QUANTITY_MISMATCH,
                            symbol=key[0],
                            exchange=key[1],
                            bot_value=json.dumps({"quantity": bot_pos.quantity}),
                            broker_value=json.dumps({"quantity": broker_pos.quantity}),
                        )
                    )

        for key, broker_pos in broker_by_symbol.items():
            if key not in bot_by_symbol:
                alerts.append(
                    self._raise_alert(
                        broker_id,
                        ReconciliationAlertType.POSITION_BROKER_ONLY,
                        symbol=key[0],
                        exchange=key[1],
                        bot_value=None,
                        broker_value=json.dumps(
                            {
                                "quantity": broker_pos.quantity,
                                "avg_price": broker_pos.average_price,
                            }
                        ),
                    )
                )

        return alerts

    def _reconcile_cash(self, broker_id: BrokerName, broker: Broker) -> list[str]:
        try:
            funds = broker.get_funds()
        except Exception:
            return []
        # Simple check: broker cash should be positive; deep mismatch flagged
        if funds.available_cash < 0:
            return [
                self._raise_alert(
                    broker_id,
                    ReconciliationAlertType.EOD_CASH_MISMATCH,
                    symbol=None,
                    exchange=None,
                    bot_value=None,
                    broker_value=json.dumps({"available_cash": funds.available_cash}),
                )
            ]
        return []

    def _load_bot_positions(
        self, broker_id: BrokerName, track_filter: str | None
    ) -> list[PositionRecord]:
        query = "SELECT * FROM positions WHERE broker_id=? AND is_open=1"
        params: list = [broker_id]
        if track_filter:
            query += " AND track=?"
            params.append(track_filter)
        rows = self._db.execute(query, params).fetchall()
        return [self._row_to_position(dict(r)) for r in rows]

    def _raise_alert(
        self,
        broker_id: BrokerName,
        alert_type: ReconciliationAlertType,
        symbol: str | None,
        exchange: str | None,
        bot_value: str | None,
        broker_value: str | None,
    ) -> str:
        alert_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            """
            INSERT INTO reconciliation_alerts
            (alert_id, broker_id, alert_type, symbol, exchange,
             bot_value, broker_value, resolved, created_at)
            VALUES (?,?,?,?,?,?,?,0,?)
            """,
            (alert_id, broker_id, alert_type, symbol, exchange, bot_value, broker_value, now),
        )
        self._db.commit()
        logger.warning(
            "Reconciliation alert %s broker=%s type=%s symbol=%s",
            alert_id,
            broker_id,
            alert_type,
            symbol,
        )
        return alert_id

    @staticmethod
    def _row_to_position(d: dict) -> PositionRecord:
        return PositionRecord(
            position_id=d["position_id"],
            symbol=d["symbol"],
            exchange=d["exchange"],
            track=d["track"],
            bucket_id=d["bucket_id"],
            broker_id=BrokerName(d["broker_id"]),
            quantity=d["quantity"],
            average_entry_price=d["average_entry_price"],
            current_price=d.get("current_price", 0.0),
            unrealised_pnl=d.get("unrealised_pnl", 0.0),
            realised_pnl=d.get("realised_pnl", 0.0),
            stop_loss_price=d["stop_loss_price"],
            target_price=d["target_price"],
            atr_at_entry=d["atr_at_entry"],
            entry_order_id=d["entry_order_id"],
            gtt_oco_id=d.get("gtt_oco_id"),
            unprotected_flag=bool(d.get("unprotected_flag", 0)),
            unprotected_since=(
                datetime.fromisoformat(d["unprotected_since"])
                if d.get("unprotected_since")
                else None
            ),
            unmanaged=bool(d.get("unmanaged", 0)),
            health_score=d.get("health_score", 100.0),
            is_open=bool(d.get("is_open", 1)),
            entry_at=datetime.fromisoformat(d["entry_at"]),
            exit_at=datetime.fromisoformat(d["exit_at"]) if d.get("exit_at") else None,
            trade_plan_id=d.get("trade_plan_id"),
            recommendation_id=d.get("recommendation_id"),
        )
