from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from capital.models import (
    CapitalLedgerRow,
    LiveCapitalView,
    LTPSource,
    Track,
    allocation_for_capital,
)

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat()


def _to_decimal(val: float | None) -> Decimal:
    if val is None:
        return Decimal("0")
    return Decimal(str(val))


def _row_to_ledger(row: sqlite3.Row) -> CapitalLedgerRow:
    return CapitalLedgerRow(
        ledger_id=row["ledger_id"],
        as_of_date=date.fromisoformat(row["as_of_date"]),
        total_capital=_to_decimal(row["total_capital"]),
        total_cash=_to_decimal(row["total_cash"]),
        long_term_allocated_pct=_to_decimal(row["long_term_allocated_pct"]),
        swing_allocated_pct=_to_decimal(row["swing_allocated_pct"]),
        intraday_allocated_pct=_to_decimal(row["intraday_allocated_pct"]),
        long_term_deployed=_to_decimal(row["long_term_deployed"]),
        swing_deployed=_to_decimal(row["swing_deployed"]),
        intraday_deployed=_to_decimal(row["intraday_deployed"]),
        high_water_mark=_to_decimal(row["high_water_mark"]),
        eod_drawdown_pct=_to_decimal(row["eod_drawdown_pct"]),
        consecutive_loss_days=int(row["consecutive_loss_days"]),
        peak_date=date.fromisoformat(row["peak_date"]),
    )


class CapitalStateManager:
    """Reads and writes capital state. Single source of truth for all capital queries."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def latest_ledger(self) -> CapitalLedgerRow | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM capital_ledger ORDER BY as_of_date DESC LIMIT 1"
            ).fetchone()
        return _row_to_ledger(row) if row else None

    def ledger_for_date(self, as_of: date) -> CapitalLedgerRow | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM capital_ledger WHERE as_of_date = ?",
                (as_of.isoformat(),),
            ).fetchone()
        return _row_to_ledger(row) if row else None

    def live_capital_view(
        self,
        ltp_source: LTPSource,
        open_positions: list[dict],
        intraday_realised_pnl_today: Decimal = Decimal("0"),
    ) -> LiveCapitalView:
        """Compute the live (intraday) capital view used for pre-trade checks.

        open_positions: list of dicts with keys:
            symbol, exchange, track, quantity, entry_price (all Decimal/str as needed)
        """
        ledger = self.latest_ledger()
        if ledger is None:
            raise RuntimeError("No capital ledger rows — initialise capital first.")

        open_position_value = Decimal("0")
        intraday_unrealised = Decimal("0")

        for pos in open_positions:
            symbol = pos["symbol"]
            exchange = pos["exchange"]
            qty = Decimal(str(pos["quantity"]))
            entry = Decimal(str(pos["entry_price"]))
            ltp = ltp_source.get_ltp(symbol, exchange)
            price = ltp if ltp is not None else entry  # stale fallback
            open_position_value += qty * price
            if pos.get("track") == Track.INTRADAY.value:
                intraday_unrealised += (price - entry) * qty

        return LiveCapitalView(
            total_cash=ledger.total_cash,
            open_position_value=open_position_value,
            hwm=ledger.high_water_mark,
            intraday_realised_pnl_today=intraday_realised_pnl_today,
            intraday_unrealised_pnl=intraday_unrealised,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def initialise(self, starting_capital: Decimal, as_of: date) -> CapitalLedgerRow:
        """Seed the first ledger row. Idempotent."""
        existing = self.ledger_for_date(as_of)
        if existing:
            return existing

        alloc = allocation_for_capital(starting_capital)
        ledger_id = str(uuid.uuid4())
        now = _now_ist()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO capital_ledger (
                    ledger_id, as_of_date,
                    total_capital, total_cash,
                    long_term_allocated_pct, swing_allocated_pct, intraday_allocated_pct,
                    long_term_deployed, swing_deployed, intraday_deployed,
                    high_water_mark, eod_drawdown_pct, consecutive_loss_days,
                    peak_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, 0, 0, ?, ?)
                """,
                (
                    ledger_id,
                    as_of.isoformat(),
                    float(starting_capital),
                    float(starting_capital),
                    float(alloc[Track.LONG_TERM]),
                    float(alloc[Track.SWING]),
                    float(alloc[Track.INTRADAY]),
                    float(starting_capital),
                    as_of.isoformat(),
                    now,
                ),
            )
        return self.ledger_for_date(as_of)  # type: ignore[return-value]

    def write_eod_ledger(
        self,
        as_of: date,
        total_capital: Decimal,
        total_cash: Decimal,
        long_term_deployed: Decimal,
        swing_deployed: Decimal,
        intraday_deployed: Decimal,
        prev_eod_pnl_net: Decimal,
    ) -> CapitalLedgerRow:
        """Write end-of-day ledger row. Called once per trading day after EOD reconciliation."""
        prev = self.latest_ledger()
        if prev is None:
            raise RuntimeError("Cannot write EOD ledger without a prior row.")

        # HWM: increases only when total_capital exceeds current HWM (performance update).
        new_hwm = max(prev.high_water_mark, total_capital)
        new_peak = as_of if total_capital > prev.high_water_mark else prev.peak_date

        drawdown = (new_hwm - total_capital) / new_hwm if new_hwm > 0 else Decimal("0")

        # Consecutive loss days: net P&L after all costs.
        consecutive = prev.consecutive_loss_days + 1 if prev_eod_pnl_net < 0 else 0

        # Use the allocation that matches this capital level.
        alloc = allocation_for_capital(total_capital)

        ledger_id = str(uuid.uuid4())
        now = _now_ist()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO capital_ledger (
                    ledger_id, as_of_date,
                    total_capital, total_cash,
                    long_term_allocated_pct, swing_allocated_pct, intraday_allocated_pct,
                    long_term_deployed, swing_deployed, intraday_deployed,
                    high_water_mark, eod_drawdown_pct, consecutive_loss_days,
                    peak_date, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ledger_id,
                    as_of.isoformat(),
                    float(total_capital),
                    float(total_cash),
                    float(alloc[Track.LONG_TERM]),
                    float(alloc[Track.SWING]),
                    float(alloc[Track.INTRADAY]),
                    float(long_term_deployed),
                    float(swing_deployed),
                    float(intraday_deployed),
                    float(new_hwm),
                    float(drawdown),
                    consecutive,
                    new_peak.isoformat(),
                    now,
                ),
            )
        return self.ledger_for_date(as_of)  # type: ignore[return-value]

    def apply_capital_flow(
        self,
        event_date: date,
        flow_type: str,
        amount: Decimal,
        notes: str | None = None,
    ) -> None:
        """Record a capital injection or withdrawal and adjust HWM accordingly.

        HWM adjusts proportionally to capital flows (Rule 2 from design).
        Positive amount = injection; negative = withdrawal.
        """
        if flow_type not in ("injection", "withdrawal", "harvest_withdrawal"):
            raise ValueError(f"Unknown flow_type: {flow_type}")

        ledger = self.latest_ledger()
        if ledger is None:
            raise RuntimeError("No capital ledger — call initialise() first.")

        hwm_adjustment = amount  # HWM moves by the same amount as the capital flow.
        event_id = str(uuid.uuid4())
        now = _now_ist()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO capital_flow_events
                    (event_id, event_date, flow_type, amount, hwm_adjustment, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_date.isoformat(),
                    flow_type,
                    float(amount),
                    float(hwm_adjustment),
                    notes,
                    now,
                ),
            )

    # ------------------------------------------------------------------
    # Circuit breaker events (audit log)
    # ------------------------------------------------------------------

    def record_circuit_breaker_trip(
        self,
        breaker_name: str,
        trip_value: Decimal,
        trip_threshold: Decimal,
    ) -> None:
        event_id = str(uuid.uuid4())
        now = _now_ist()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO circuit_breaker_events
                    (event_id, breaker_name, event_type, trip_value, trip_threshold,
                     reset_reason, event_time, created_at)
                VALUES (?, ?, 'tripped', ?, ?, NULL, ?, ?)
                """,
                (event_id, breaker_name, float(trip_value), float(trip_threshold), now, now),
            )

    def record_circuit_breaker_reset(self, breaker_name: str, reason: str) -> None:
        event_id = str(uuid.uuid4())
        now = _now_ist()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO circuit_breaker_events
                    (event_id, breaker_name, event_type, trip_value, trip_threshold,
                     reset_reason, event_time, created_at)
                VALUES (?, ?, 'reset', NULL, NULL, ?, ?, ?)
                """,
                (event_id, breaker_name, reason, now, now),
            )
