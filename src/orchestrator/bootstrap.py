"""Make a fresh database tradable (#74).

Two prerequisites had no production writer:
- `risk_config`: only `RiskConfigStore.seed_defaults()` creates version 1, and only
  tests called it (migration 0011 copies the latest row, so it adds nothing to an
  empty table). Without it the watchlist task fails and the session's breakers
  fail closed all day.
- `capital_ledger`: first written by the 16:00 EOD sync, so day one had no
  capital and no execution.

Both are seeded only when missing, never overwritten. Capital comes from the Kite
account's available cash (same source as the EOD sync), which needs a valid token,
so it runs after the 08:45 token refresh (and at boot, for a mid-day restart).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)


def seed_risk_config(db_path: str, today: date) -> bool:
    """Insert the default risk config if the table is empty. True if seeded."""
    from src.capital.risk_config import RiskConfigStore

    with sqlite3.connect(db_path, timeout=10) as conn:
        if conn.execute("SELECT COUNT(*) FROM risk_config").fetchone()[0]:
            return False
    RiskConfigStore(db_path).seed_defaults(today)
    logger.warning("risk_config_seeded defaults as version 1 effective=%s", today)
    return True


def bootstrap_capital(db_path: str, brokers: list, today: date, alert=None) -> bool:
    """Create the first capital ledger row from broker cash if none exists.
    True if a ledger exists afterwards. Never overwrites an existing ledger."""
    from src.capital.state import CapitalStateManager

    mgr = CapitalStateManager(db_path)
    if mgr.latest_ledger() is not None:
        return True
    total = Decimal("0")
    for broker in brokers:
        try:
            total += Decimal(str(broker.get_funds().available_cash))
        except Exception as exc:  # expired token before 08:45, network, ...
            logger.warning("bootstrap_funds_failed broker=%s error=%s", broker.broker_id, exc)
    if total <= 0:
        msg = "No capital ledger and no broker cash available: execution stays off."
        logger.error("capital_bootstrap_failed %s", msg)
        if alert:
            alert("No capital ledger", msg)
        return False
    mgr.initialise(total, today - timedelta(days=1))  # 'yesterday's close'; EOD writes today
    logger.warning("capital_bootstrapped total=%.2f source=broker_cash", float(total))
    if alert:
        alert(
            "Capital ledger initialised",
            f"Starting capital ₹{total:,.2f} from the Kite account's available cash.",
        )
    return True
