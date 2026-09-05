from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from capital.models import RiskConfig, Track

IST = ZoneInfo("Asia/Kolkata")

# Single source of truth for Phase 1 design-specified defaults.
# seed_defaults() inserts these as version 1. To change a live value,
# create a new version via update_live_backtest_ratio() or a manual INSERT.
# MIN_RR and ATR_K are co-located design thresholds — see capital/models.py.
RISK_CONFIG_DEFAULTS: dict[str, object] = {
    "risk_per_intraday_trade_pct": Decimal("0.005"),
    "intraday_daily_loss_limit_pct": Decimal("0.020"),
    "portfolio_daily_loss_limit_pct": Decimal("0.020"),
    "portfolio_weekly_loss_limit_pct": Decimal("0.040"),
    "portfolio_max_drawdown_pct": Decimal("0.080"),
    "single_stock_cap_pct": Decimal("0.050"),
    "sector_cap_pct": Decimal("0.250"),
    "correlation_cluster_cap_pct": Decimal("0.350"),
    "intraday_consecutive_loss_count": 3,
    "nifty_intraday_pause_pct": Decimal("0.030"),
    "live_backtest_ratio_intraday": Decimal("0.70"),
    "sentiment_confidence_threshold": Decimal("0.60"),
    "min_stock_price": Decimal("100"),
    "min_avg_daily_volume": 500000,
    "min_avg_daily_turnover_cr": Decimal("5.0"),
}


def _row_to_risk_config(row: sqlite3.Row) -> RiskConfig:
    return RiskConfig(
        config_id=row["config_id"],
        version=row["version"],
        effective_from=date.fromisoformat(row["effective_from"]),
        risk_per_intraday_trade_pct=Decimal(str(row["risk_per_intraday_trade_pct"])),
        intraday_daily_loss_limit_pct=Decimal(str(row["intraday_daily_loss_limit_pct"])),
        portfolio_daily_loss_limit_pct=Decimal(str(row["portfolio_daily_loss_limit_pct"])),
        portfolio_weekly_loss_limit_pct=Decimal(str(row["portfolio_weekly_loss_limit_pct"])),
        portfolio_max_drawdown_pct=Decimal(str(row["portfolio_max_drawdown_pct"])),
        single_stock_cap_pct=Decimal(str(row["single_stock_cap_pct"])),
        sector_cap_pct=Decimal(str(row["sector_cap_pct"])),
        correlation_cluster_cap_pct=Decimal(str(row["correlation_cluster_cap_pct"])),
        intraday_consecutive_loss_count=int(row["intraday_consecutive_loss_count"]),
        nifty_intraday_pause_pct=Decimal(str(row["nifty_intraday_pause_pct"])),
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
            now = datetime.now(IST).replace(tzinfo=None).isoformat()
            d = RISK_CONFIG_DEFAULTS
            conn.execute(
                """
                INSERT INTO risk_config (
                    config_id, version, effective_from,
                    risk_per_intraday_trade_pct,
                    intraday_daily_loss_limit_pct,
                    portfolio_daily_loss_limit_pct, portfolio_weekly_loss_limit_pct,
                    portfolio_max_drawdown_pct,
                    single_stock_cap_pct, sector_cap_pct, correlation_cluster_cap_pct,
                    intraday_consecutive_loss_count,
                    nifty_intraday_pause_pct,
                    live_backtest_ratio_intraday,
                    sentiment_confidence_threshold,
                    min_stock_price, min_avg_daily_volume, min_avg_daily_turnover_cr,
                    created_at
                ) VALUES (
                    ?, 1, ?,
                    ?,
                    ?,
                    ?, ?,
                    ?,
                    ?, ?, ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?, ?, ?,
                    ?
                )
                """,
                (
                    config_id, effective_from.isoformat(),
                    float(d["risk_per_intraday_trade_pct"]),
                    float(d["intraday_daily_loss_limit_pct"]),
                    float(d["portfolio_daily_loss_limit_pct"]),
                    float(d["portfolio_weekly_loss_limit_pct"]),
                    float(d["portfolio_max_drawdown_pct"]),
                    float(d["single_stock_cap_pct"]),
                    float(d["sector_cap_pct"]),
                    float(d["correlation_cluster_cap_pct"]),
                    d["intraday_consecutive_loss_count"],
                    float(d["nifty_intraday_pause_pct"]),
                    float(d["live_backtest_ratio_intraday"]),
                    float(d["sentiment_confidence_threshold"]),
                    float(d["min_stock_price"]),
                    d["min_avg_daily_volume"],
                    float(d["min_avg_daily_turnover_cr"]),
                    now,
                ),
            )
        return self.load_version(1)

    def update_live_backtest_ratio(
        self, track: Track, new_ratio: Decimal, effective_from: date
    ) -> RiskConfig:
        """Create a new config version with an updated confidence haircut ratio.

        Called after 60 days of live trading once actual win rates are measured.
        """
        current = self.load_current()

        with self._conn() as conn:
            config_id = str(uuid.uuid4())
            new_version = current.version + 1
            now = datetime.now(IST).replace(tzinfo=None).isoformat()
            conn.execute(
                """
                INSERT INTO risk_config (
                    config_id, version, effective_from,
                    risk_per_intraday_trade_pct,
                    intraday_daily_loss_limit_pct,
                    portfolio_daily_loss_limit_pct, portfolio_weekly_loss_limit_pct,
                    portfolio_max_drawdown_pct,
                    single_stock_cap_pct, sector_cap_pct, correlation_cluster_cap_pct,
                    intraday_consecutive_loss_count,
                    nifty_intraday_pause_pct,
                    live_backtest_ratio_intraday,
                    sentiment_confidence_threshold,
                    created_at,
                    min_stock_price, min_avg_daily_volume, min_avg_daily_turnover_cr
                ) SELECT
                    ?, ?, ?,
                    risk_per_intraday_trade_pct,
                    intraday_daily_loss_limit_pct,
                    portfolio_daily_loss_limit_pct, portfolio_weekly_loss_limit_pct,
                    portfolio_max_drawdown_pct,
                    single_stock_cap_pct, sector_cap_pct, correlation_cluster_cap_pct,
                    intraday_consecutive_loss_count,
                    nifty_intraday_pause_pct,
                    ?,
                    sentiment_confidence_threshold,
                    ?,
                    min_stock_price, min_avg_daily_volume, min_avg_daily_turnover_cr
                FROM risk_config WHERE version = ?
                """,
                (
                    config_id,
                    new_version,
                    effective_from.isoformat(),
                    float(new_ratio),
                    now,
                    current.version,
                ),
            )
        return self.load_version(new_version)
