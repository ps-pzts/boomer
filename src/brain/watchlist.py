"""Daily intraday watchlist: the N stocks the live cycle watches (design §3.6, Q3).

Selection, run once pre-market on batch (point-in-time) features only:
  1. Eligibility: risk_config price / volume / turnover floors, a usable
     atr_14d (the opening-range feature and trade sizing need it), and the
     signal generator returning a signal (its own liquidity gate, see #38).
  2. LONG candidates, ranked by the generator's batch-only raw score.
  3. Remaining slots filled with the most liquid eligible names: live features
     (gap, opening range, NIFTY direction) can turn a flat pre-market score into
     a trade, so a quiet news day still yields a full watchlist.

Every rejection is counted by reason and logged (CLAUDE.md §9 attribution).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from brain.models import Direction
from brain.signals.intraday import IntradaySignalGenerator

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

WATCHLIST_SIZE = 50  # design Q3
_RUPEES_PER_CRORE = Decimal("10000000")


@dataclass(frozen=True)
class WatchlistEntry:
    stock_symbol: str
    exchange: str
    rank: int  # 1 = first
    selection_reason: str  # "batch_score" | "liquidity_fill"
    batch_raw_score: float | None
    batch_direction: str | None
    avg_traded_value_20d: float


@dataclass(frozen=True)
class _Candidate:
    symbol: str
    exchange: str
    raw_score: float
    direction: Direction
    avg_traded_value: float


def eligibility_reason(features: dict[str, Any], risk_config: Any) -> str | None:
    """None if eligible, else the first rule that rejects the symbol."""
    price = features.get("price_close")
    if price is None or Decimal(str(price)) < risk_config.min_stock_price:
        return "price_below_min"
    volume = features.get("avg_daily_volume_20d")
    if volume is None or Decimal(str(volume)) < risk_config.min_avg_daily_volume:
        return "volume_below_min"
    value = features.get("avg_traded_value_20d")
    if (
        value is None
        or Decimal(str(value)) / _RUPEES_PER_CRORE < risk_config.min_avg_daily_turnover_cr
    ):
        return "turnover_below_min"
    atr = features.get("atr_14d")
    if atr is None or float(atr) <= 0:
        return "no_atr"
    return None


def select_watchlist(
    features_by_symbol: Iterable[tuple[str, str, dict[str, Any]]],
    risk_config: Any,
    regime: str,
    now: datetime,
    size: int = WATCHLIST_SIZE,
    generator: IntradaySignalGenerator | None = None,
) -> list[WatchlistEntry]:
    """Rank the universe and return the top `size` entries (fewer if not enough are eligible)."""
    gen = generator or IntradaySignalGenerator()
    rejected: Counter[str] = Counter()
    candidates: list[_Candidate] = []

    for symbol, exchange, features in features_by_symbol:
        reason = eligibility_reason(features, risk_config)
        if reason is None:
            signal = gen.generate(symbol, exchange, features, regime, now)
            if signal is None:
                reason = "signal_gate"
            else:
                candidates.append(
                    _Candidate(
                        symbol,
                        exchange,
                        signal.raw_score,
                        signal.direction,
                        float(features["avg_traded_value_20d"]),
                    )
                )
        if reason is not None:
            rejected[reason] += 1

    longs = sorted(
        (c for c in candidates if c.direction == Direction.LONG),
        key=lambda c: (-c.raw_score, -c.avg_traded_value, c.symbol),
    )
    picked = longs[:size]
    picked_keys = {(c.symbol, c.exchange) for c in picked}
    fill = sorted(
        (c for c in candidates if (c.symbol, c.exchange) not in picked_keys),
        key=lambda c: (-c.avg_traded_value, c.symbol),
    )[: size - len(picked)]

    entries = [
        WatchlistEntry(
            c.symbol,
            c.exchange,
            i + 1,
            "batch_score" if i < len(picked) else "liquidity_fill",
            c.raw_score,
            c.direction.value,
            c.avg_traded_value,
        )
        for i, c in enumerate(picked + fill)
    ]
    logger.info(
        "watchlist_selected size=%d by_score=%d by_liquidity=%d eligible=%d rejected=%s",
        len(entries),
        len(picked),
        len(fill),
        len(candidates),
        dict(rejected),
    )
    return entries


class WatchlistStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, session_date: date, entries: list[WatchlistEntry]) -> None:
        """Replace the session's watchlist (re-running the task the same day is safe)."""
        now = datetime.now(IST).isoformat()
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM intraday_watchlist WHERE session_date = ?", (session_date.isoformat(),)
            )
            conn.executemany(
                """INSERT INTO intraday_watchlist (session_date, stock_symbol, exchange, rank,
                       selection_reason, batch_raw_score, batch_direction,
                       avg_traded_value_20d, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        session_date.isoformat(),
                        e.stock_symbol,
                        e.exchange,
                        e.rank,
                        e.selection_reason,
                        e.batch_raw_score,
                        e.batch_direction,
                        e.avg_traded_value_20d,
                        now,
                    )
                    for e in entries
                ],
            )

    def load(self, session_date: date) -> list[WatchlistEntry]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM intraday_watchlist WHERE session_date = ? ORDER BY rank",
                (session_date.isoformat(),),
            ).fetchall()
        return [
            WatchlistEntry(
                r["stock_symbol"],
                r["exchange"],
                r["rank"],
                r["selection_reason"],
                r["batch_raw_score"],
                r["batch_direction"],
                r["avg_traded_value_20d"],
            )
            for r in rows
        ]
