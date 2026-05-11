from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from capital.models import RiskConfig, Track


def _row_to_risk_config(row: sqlite3.Row) -> RiskConfig:
    return RiskConfig(
        config_id=row["config_id"],
        version=row["version"],
        effective_from=date.fromisoformat(row["effective_from"]),
        risk_per_intraday_trade_pct=Decimal(str(row["risk_per_intraday_trade_pct"])),
        risk_per_swing_trade_pct=Decimal(str(row["risk_per_swing_trade_pct"])),
        risk_per_long_term_trade_pct=Decimal(str(row["risk_per_long_term_trade_pct"])),
        intraday_daily_loss_limit_pct=Decimal(str(row["intraday_daily_loss_limit_pct"])),
        swing_weekly_loss_limit_pct=Decimal(str(row["swing_weekly_loss_limit_pct"])),
        portfolio_daily_loss_limit_pct=Decimal(str(row["portfolio_daily_loss_limit_pct"])),
        portfolio_weekly_loss_limit_pct=Decimal(str(row["portfolio_weekly_loss_limit_pct"])),
        portfolio_max_drawdown_pct=Decimal(str(row["portfolio_max_drawdown_pct"])),
        single_stock_cap_pct=Decimal(str(row["single_stock_cap_pct"])),
        sector_cap_pct=Decimal(str(row["sector_cap_pct"])),
        correlation_cluster_cap_pct=Decimal(str(row["correlation_cluster_cap_pct"])),
        intraday_consecutive_loss_count=int(row["intraday_consecutive_loss_count"]),
        swing_30d_loss_count=int(row["swing_30d_loss_count"]),
        nifty_intraday_pause_pct=Decimal(str(row["nifty_intraday_pause_pct"])),
        live_backtest_ratio_long_term=Decimal(str(row["live_backtest_ratio_long_term"])),
        live_backtest_ratio_swing=Decimal(str(row["live_backtest_ratio_swing"])),
        live_backtest_ratio_intraday=Decimal(str(row["live_backtest_ratio_intraday"])),
        sentiment_confidence_threshold=Decimal(str(row["sentiment_confidence_threshold"])),
        min_stock_price=Decimal(str(row["min_stock_price"])),
        min_avg_daily_volume=int(row["min_avg_daily_volume"]),
        min_avg_daily_turnover_cr=Decimal(str(row["min_avg_daily_turnover_cr"])),
    )


class RiskConfigStore:
    """Read and write versioned risk configuration."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def load_current(self) -> RiskConfig:
        """Return the highest-version risk config."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM risk_config ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("No risk_config rows found — run seed_defaults() first.")
        return _row_to_risk_config(row)

    def load_version(self, version: int) -> RiskConfig:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM risk_config WHERE version = ?", (version,)).fetchone()
        if row is None:
            raise KeyError(f"risk_config version {version} not found.")
        return _row_to_risk_config(row)

    def seed_defaults(self, effective_from: date) -> RiskConfig:
        """Insert version 1 with design-specified defaults. Idempotent."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT config_id FROM risk_config WHERE version = 1"
            ).fetchone()
            if existing:
                return self.load_version(1)

            config_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO risk_config (
                    config_id, version, effective_from,
                    risk_per_intraday_trade_pct, risk_per_swing_trade_pct,
                    risk_per_long_term_trade_pct,
                    intraday_daily_loss_limit_pct, swing_weekly_loss_limit_pct,
                    portfolio_daily_loss_limit_pct, portfolio_weekly_loss_limit_pct,
                    portfolio_max_drawdown_pct,
                    single_stock_cap_pct, sector_cap_pct, correlation_cluster_cap_pct,
                    intraday_consecutive_loss_count, swing_30d_loss_count,
                    nifty_intraday_pause_pct,
                    live_backtest_ratio_long_term, live_backtest_ratio_swing,
                    live_backtest_ratio_intraday,
                    sentiment_confidence_threshold,
                    min_stock_price, min_avg_daily_volume, min_avg_daily_turnover_cr,
                    created_at
                ) VALUES (
                    ?, 1, ?,
                    0.005, 0.010, 0.010,
                    0.020, 0.040,
                    0.020, 0.040, 0.080,
                    0.050, 0.250, 0.350,
                    3, 4,
                    0.030,
                    0.70, 0.70, 0.70,
                    0.60,
                    100, 500000, 5.0,
                    ?
                )
                """,
                (config_id, effective_from.isoformat(), now),
            )
        return self.load_version(1)

    def update_live_backtest_ratio(
        self, track: Track, new_ratio: Decimal, effective_from: date
    ) -> RiskConfig:
        """Create a new config version with an updated per-track haircut ratio.

        Called after 60 days of live trading per track once actual win rates are measured.
        """
        current = self.load_current()
        field_map = {
            Track.LONG_TERM: "live_backtest_ratio_long_term",
            Track.SWING: "live_backtest_ratio_swing",
            Track.INTRADAY: "live_backtest_ratio_intraday",
        }
        updated = {
            "live_backtest_ratio_long_term": current.live_backtest_ratio_long_term,
            "live_backtest_ratio_swing": current.live_backtest_ratio_swing,
            "live_backtest_ratio_intraday": current.live_backtest_ratio_intraday,
        }
        updated[field_map[track]] = new_ratio

        with self._conn() as conn:
            config_id = str(uuid.uuid4())
            new_version = current.version + 1
            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO risk_config SELECT
                    ?, ?, ?,
                    risk_per_intraday_trade_pct, risk_per_swing_trade_pct,
                    risk_per_long_term_trade_pct,
                    intraday_daily_loss_limit_pct, swing_weekly_loss_limit_pct,
                    portfolio_daily_loss_limit_pct, portfolio_weekly_loss_limit_pct,
                    portfolio_max_drawdown_pct,
                    single_stock_cap_pct, sector_cap_pct, correlation_cluster_cap_pct,
                    intraday_consecutive_loss_count, swing_30d_loss_count,
                    nifty_intraday_pause_pct,
                    ?, ?, ?,
                    sentiment_confidence_threshold,
                    ?
                FROM risk_config WHERE version = ?
                """,
                (
                    config_id,
                    new_version,
                    effective_from.isoformat(),
                    float(updated["live_backtest_ratio_long_term"]),
                    float(updated["live_backtest_ratio_swing"]),
                    float(updated["live_backtest_ratio_intraday"]),
                    now,
                    current.version,
                ),
            )
        return self.load_version(new_version)
