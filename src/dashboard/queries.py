"""Read-only dashboard queries. Never used for writes.

All queries run on a separate WAL-mode read connection so they
never contend with the writer connections in other modules.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class TodaySnapshot:
    total_pnl: float
    signals_generated: int
    trades_placed: int
    positions_opened: int
    intraday_open: int
    bot_mode: str
    circuit_breakers_tripped: list[str]
    missed_critical_alerts: int


@dataclass
class PositionRow:
    position_id: str
    symbol: str
    track: str
    entry_date: str
    days_held: int
    entry_price: float
    current_price: float
    pnl: float
    pnl_pct: float
    stop_loss: float
    target: float
    health_score: float
    exit_recommendation: str | None


@dataclass
class CapitalView:
    total_capital: float
    hwm: float
    drawdown_pct: float
    intraday_allocated: float
    intraday_deployed: float


@dataclass
class TaskRunRow:
    task_id: str
    run_date: str
    status: str
    started_at: str | None
    ended_at: str | None
    error_message: str | None


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    return conn


def get_today_snapshot(db_path: str, run_date: str) -> TodaySnapshot:
    conn = _conn(db_path)
    try:
        mode_row = conn.execute("SELECT mode FROM bot_mode WHERE id=1").fetchone()
        bot_mode = mode_row["mode"] if mode_row else "auto"

        pnl_row = conn.execute(
            "SELECT COALESCE(SUM(realised_pnl),0) as pnl FROM positions WHERE DATE(entry_at)=?",
            (run_date,),
        ).fetchone()

        signals_row = conn.execute(
            "SELECT COUNT(*) as n FROM signals WHERE DATE(generated_at)=?", (run_date,)
        ).fetchone()

        trades_row = conn.execute(
            "SELECT COUNT(*) as n FROM orders"
            " WHERE DATE(created_at)=? AND status NOT IN ('CANCELLED','REJECTED')",
            (run_date,),
        ).fetchone()

        pos_opened_row = conn.execute(
            "SELECT COUNT(*) as n FROM positions WHERE DATE(entry_at)=?", (run_date,)
        ).fetchone()

        id_row = conn.execute(
            "SELECT COUNT(*) as n FROM positions WHERE is_open=1 AND track='intraday'"
        ).fetchone()

        missed_row = conn.execute(
            "SELECT COUNT(*) as n FROM critical_notification_failures WHERE acknowledged=0"
        ).fetchone()

        # Circuit breakers tripped today
        cb_rows = conn.execute(
            """SELECT DISTINCT breaker_name FROM circuit_breaker_events
               WHERE event_type='tripped' AND DATE(event_time)=?""",
            (run_date,),
        ).fetchall()
        tripped = [r["breaker_name"] for r in cb_rows]
    finally:
        conn.close()

    return TodaySnapshot(
        total_pnl=float(pnl_row["pnl"]),
        signals_generated=signals_row["n"],
        trades_placed=trades_row["n"],
        positions_opened=pos_opened_row["n"],
        intraday_open=id_row["n"],
        bot_mode=bot_mode,
        circuit_breakers_tripped=tripped,
        missed_critical_alerts=missed_row["n"],
    )


def get_open_positions(db_path: str) -> list[PositionRow]:
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """SELECT p.position_id, p.symbol, p.track, p.entry_at,
                      julianday('now') - julianday(p.entry_at) as days_held,
                      p.average_entry_price,
                      COALESCE(
                          (SELECT close FROM prices
                           WHERE stock_symbol=p.symbol AND exchange=p.exchange
                           ORDER BY trade_date DESC LIMIT 1),
                          p.average_entry_price
                      ) as current_price,
                      p.realised_pnl, p.quantity,
                      p.stop_loss_price, p.target_price,
                      COALESCE(p.health_score, 50.0) as health_score
               FROM positions p
               WHERE p.is_open = 1
               ORDER BY p.health_score ASC""",
        ).fetchall()
    finally:
        conn.close()

    result = []
    for r in rows:
        entry = float(r["average_entry_price"])
        cur = float(r["current_price"])
        qty = r["quantity"] or 0
        pnl = (cur - entry) * qty
        pnl_pct = ((cur - entry) / entry * 100) if entry else 0.0
        result.append(
            PositionRow(
                position_id=r["position_id"],
                symbol=r["symbol"],
                track=r["track"],
                entry_date=r["entry_at"][:10] if r["entry_at"] else "",
                days_held=int(r["days_held"] or 0),
                entry_price=entry,
                current_price=cur,
                pnl=pnl,
                pnl_pct=pnl_pct,
                stop_loss=float(r["stop_loss_price"] or 0),
                target=float(r["target_price"] or 0),
                health_score=float(r["health_score"]),
                exit_recommendation=None,
            )
        )
    return result


def get_capital_view(db_path: str) -> CapitalView:
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM capital_ledger ORDER BY as_of_date DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return CapitalView(0, 0, 0, 0, 0)
    hwm = float(row["high_water_mark"] or 0)
    total = float(row["total_capital"] or 0)
    dd = ((hwm - total) / hwm * 100) if hwm > 0 else 0.0
    # allocated_pct is a fraction (0.0-1.0), not a percentage — no /100 needed.
    id_alloc = total * float(row["intraday_allocated_pct"] or 0)
    return CapitalView(
        total_capital=total,
        hwm=hwm,
        drawdown_pct=dd,
        intraday_allocated=id_alloc,
        intraday_deployed=float(row["intraday_deployed"] or 0),
    )


def get_recent_task_runs(db_path: str, hours: int = 24) -> list[TaskRunRow]:
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """SELECT task_id, run_date, status, started_at, ended_at, error_message
               FROM task_runs
               WHERE started_at >= datetime('now', ?)
               ORDER BY started_at DESC""",
            (f"-{hours} hours",),
        ).fetchall()
    finally:
        conn.close()
    return [
        TaskRunRow(
            task_id=r["task_id"],
            run_date=r["run_date"],
            status=r["status"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            error_message=r["error_message"],
        )
        for r in rows
    ]


def get_recent_errors(db_path: str, limit: int = 50, hours: int = 48) -> list[dict]:
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """SELECT task_id, run_date, started_at, error_message, error_traceback
               FROM task_runs
               WHERE error_message IS NOT NULL
                 AND started_at >= datetime('now', ?)
               ORDER BY started_at DESC LIMIT ?""",
            (f"-{hours} hours", limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
