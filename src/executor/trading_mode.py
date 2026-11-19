"""Paper/live trading-mode switch (designs/intraday-live-pipeline.md §5).

Live needs two independent keys: the BOOMER_TRADING_MODE env var set to "live"
AND an enabled row in live_trading_authorisation. Anything else (unset env,
typo, missing table, no row, DB error) resolves to paper. Fail closed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from executor.brokers.base import Broker
from executor.brokers.feeds.base import MarketDataFeed
from executor.brokers.paper_broker import PaperBroker

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

ENV_VAR = "BOOMER_TRADING_MODE"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class ModeDecision:
    mode: TradingMode
    reason: str


class LiveTradingAuthorisationStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def is_enabled(self) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT enabled FROM live_trading_authorisation ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return bool(row and row["enabled"] == 1)

    def set_enabled(self, enabled: bool, actor: str, reason: str) -> None:
        if not actor or not reason:
            raise ValueError("actor and reason are required for an auditable change")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO live_trading_authorisation (enabled, actor, reason, created_at)"
                " VALUES (?, ?, ?, ?)",
                (1 if enabled else 0, actor, reason, datetime.now(IST).isoformat()),
            )
        logger.warning(
            "live_trading_authorisation_changed enabled=%s actor=%s reason=%s",
            enabled,
            actor,
            reason,
        )


def resolve_trading_mode(
    env_value: str | None, store: LiveTradingAuthorisationStore
) -> ModeDecision:
    """Decide the mode and log which rule decided it (CLAUDE.md §9 attribution)."""
    requested = (env_value or "").strip().lower()
    if requested != TradingMode.LIVE:
        decision = ModeDecision(TradingMode.PAPER, f"{ENV_VAR}={env_value!r} (not 'live')")
    else:
        try:
            authorised = store.is_enabled()
        except Exception as exc:  # missing table, locked DB, ...: fail closed
            decision = ModeDecision(TradingMode.PAPER, f"authorisation check failed: {exc}")
        else:
            decision = (
                ModeDecision(TradingMode.LIVE, "env=live and DB authorisation enabled")
                if authorised
                else ModeDecision(TradingMode.PAPER, "env=live but DB authorisation not enabled")
            )
    logger.info("trading_mode_resolved mode=%s reason=%s", decision.mode, decision.reason)
    return decision


def build_execution_broker(
    mode: TradingMode,
    feed: MarketDataFeed,
    live_broker: Broker | None,
    paper_initial_cash: float,
    paper_book: object | None = None,
) -> Broker:
    """Paper: simulated fills on the live feed. Live: the real broker."""
    if mode == TradingMode.LIVE:
        if live_broker is None:
            raise RuntimeError("live mode requested but no live broker is configured")
        return live_broker
    if paper_initial_cash <= 0:
        raise ValueError("paper_initial_cash must be positive")
    return PaperBroker(price_source=feed, initial_cash=paper_initial_cash, book=paper_book)
