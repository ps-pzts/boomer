from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


class FeatureStore:
    """Stage 0 — point-in-time feature storage and retrieval.

    Every query enforces: WHERE valid_from <= as_of AND source_max_observed_at <= as_of
    This is the single rule that prevents lookahead bias in backtesting.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def write_feature(
        self,
        stock_symbol: str,
        exchange: str,
        feature_name: str,
        feature_value: float,
        valid_from: date,
        source_max_observed_at: date,
        metadata: dict[str, Any] | None = None,
        computer_version: str = "1.0",
    ) -> str:
        """Write a feature row. Supersedes any existing row for same symbol+name+valid_from."""
        feature_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        valid_from_str = valid_from.isoformat()
        observed_str = source_max_observed_at.isoformat()
        metadata_json = json.dumps(metadata) if metadata else None

        with self._conn() as conn:
            # Supersede any current row for this exact symbol/feature/valid_from
            conn.execute(
                """
                UPDATE features SET valid_to = ?
                WHERE stock_symbol = ? AND exchange = ? AND feature_name = ?
                  AND valid_from = ? AND valid_to IS NULL
                """,
                (now, stock_symbol, exchange, feature_name, valid_from_str),
            )
            conn.execute(
                """
                INSERT INTO features (
                    feature_id, stock_symbol, exchange, feature_name,
                    feature_value, feature_metadata, valid_from, valid_to,
                    source_max_observed_at, computer_version, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    feature_id,
                    stock_symbol,
                    exchange,
                    feature_name,
                    feature_value,
                    metadata_json,
                    valid_from_str,
                    observed_str,
                    computer_version,
                    now,
                ),
            )
        return feature_id

    def get_features_as_of(
        self,
        stock_symbol: str,
        exchange: str,
        as_of_date: date,
    ) -> dict[str, float]:
        """Return all current features for a stock as of a given date.

        Uses point-in-time filter: valid_from <= as_of AND source_max_observed_at <= as_of.
        Returns only the most recent valid_from value per feature_name.
        """
        as_of_str = as_of_date.isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT feature_name, feature_value
                FROM features
                WHERE stock_symbol = ?
                  AND exchange = ?
                  AND valid_from <= ?
                  AND source_max_observed_at <= ?
                  AND valid_to IS NULL
                ORDER BY feature_name, valid_from DESC
                """,
                (stock_symbol, exchange, as_of_str, as_of_str),
            ).fetchall()

        seen: set[str] = set()
        result: dict[str, float] = {}
        for row in rows:
            name = row["feature_name"]
            if name not in seen:
                result[name] = row["feature_value"]
                seen.add(name)
        return result

    def get_feature_as_of(
        self,
        stock_symbol: str,
        exchange: str,
        feature_name: str,
        as_of_date: date,
    ) -> float | None:
        """Return the single most recent value for one feature as of a date."""
        as_of_str = as_of_date.isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT feature_value FROM features
                WHERE stock_symbol = ? AND exchange = ? AND feature_name = ?
                  AND valid_from <= ? AND source_max_observed_at <= ?
                  AND valid_to IS NULL
                ORDER BY valid_from DESC
                LIMIT 1
                """,
                (stock_symbol, exchange, feature_name, as_of_str, as_of_str),
            ).fetchone()
        return float(row["feature_value"]) if row else None

    def get_latest_feature(
        self,
        stock_symbol: str,
        exchange: str,
        feature_name: str,
    ) -> float | None:
        """Return the latest value regardless of date — for live (non-backtest) use only."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT feature_value FROM features
                WHERE stock_symbol = ? AND exchange = ? AND feature_name = ?
                  AND valid_to IS NULL
                ORDER BY valid_from DESC
                LIMIT 1
                """,
                (stock_symbol, exchange, feature_name),
            ).fetchone()
        return float(row["feature_value"]) if row else None

    def list_symbols_with_feature(
        self,
        feature_name: str,
        as_of_date: date,
        exchange: str = "NSE",
    ) -> list[str]:
        """Return all symbols that have a given feature current as of a date."""
        as_of_str = as_of_date.isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT stock_symbol FROM features
                WHERE exchange = ? AND feature_name = ?
                  AND valid_from <= ? AND source_max_observed_at <= ?
                  AND valid_to IS NULL
                """,
                (exchange, feature_name, as_of_str, as_of_str),
            ).fetchall()
        return [r["stock_symbol"] for r in rows]
