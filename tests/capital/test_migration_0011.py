"""Migration 0011 (#38): new risk_config version with a ₹10 cr turnover floor."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from capital.risk_config import RiskConfigStore

_MIGRATIONS = Path(__file__).parents[2] / "migrations"
_SQL_0011 = (_MIGRATIONS / "0011_min_turnover_10cr.sql").read_text()


def _db_before_0011(tmp_path: Path) -> str:
    path = str(tmp_path / "rc.db")
    conn = sqlite3.connect(path)
    for f in sorted(_MIGRATIONS.glob("*.sql")):
        if f.name < "0011":
            conn.executescript(f.read_text())
    conn.close()
    return path


def test_existing_five_crore_config_gets_new_version(tmp_path):
    db = _db_before_0011(tmp_path)
    store = RiskConfigStore(db)
    store.seed_defaults(date(2026, 1, 1))
    conn = sqlite3.connect(db)
    conn.execute("UPDATE risk_config SET min_avg_daily_turnover_cr = 5.0 WHERE version = 1")
    conn.commit()
    v1 = store.load_version(1)

    conn.executescript(_SQL_0011)
    conn.close()

    current = store.load_current()
    assert current.version == 2
    assert current.min_avg_daily_turnover_cr == Decimal("10.0")
    # every other setting copied unchanged (explicit column list, see #25)
    for field in ("risk_per_intraday_trade_pct", "intraday_daily_loss_limit_pct",
                  "min_stock_price", "min_avg_daily_volume", "nifty_intraday_pause_pct",
                  "intraday_consecutive_loss_count", "live_backtest_ratio_intraday"):  # fmt: skip
        assert getattr(current, field) == getattr(v1, field), field
    assert store.load_version(1).min_avg_daily_turnover_cr == Decimal("5.0")  # history kept


def test_already_ten_crore_adds_no_version(tmp_path):
    db = _db_before_0011(tmp_path)
    RiskConfigStore(db).seed_defaults(date(2026, 1, 1))  # defaults are 10.0 now
    conn = sqlite3.connect(db)
    conn.executescript(_SQL_0011)
    assert conn.execute("SELECT MAX(version) FROM risk_config").fetchone()[0] == 1


def test_fresh_database_untouched(tmp_path):
    db = _db_before_0011(tmp_path)
    conn = sqlite3.connect(db)
    conn.executescript(_SQL_0011)
    assert conn.execute("SELECT COUNT(*) FROM risk_config").fetchone()[0] == 0
    assert RiskConfigStore(db).seed_defaults(date(2026, 1, 1)).min_avg_daily_turnover_cr == (
        Decimal("10.0")
    )
