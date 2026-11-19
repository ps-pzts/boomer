"""The live projection and timelines behind the observability UI (#105, OBS-3).

Reads the `events` table (only src/observability may) plus each machine's own state
table for *current* values, so a missed event is self-correcting: the cards show what
the tables say now, the stream and timelines show what happened.

    live(db_path)                    -> the whole live screen, one JSON-safe dict
    trade_timeline(db_path, id)      -> one trade's story, every event in order
    session_timeline(db_path, date)  -> a day: its trades and its non-routine events
    days(db_path, n)                 -> daily results for the day chart

Derived attention items (designs/observability.md §10.3): session halted, position
without a confirmed stop, trade stuck in PROTECTING, a SILENT component, a gap in a
component's event sequence, critical events in the last hour. Broker drift needs
`broker_snapshot` events, which are not emitted yet (OBS-2); `drift` says so.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from observability import describe

IST = ZoneInfo("Asia/Kolkata")
HEARTBEAT_SECONDS = 30
SILENT_AFTER_SECONDS = 3 * HEARTBEAT_SECONDS  # §7: 3 missed beats
PROTECTING_TOO_LONG_SECONDS = 120
HOURS = (time(9, 0), time(15, 45))  # components must beat during these hours
STREAM_LIMIT = 60
TIMELINE_LIMIT = 500
_ROUTINE_KINDS = ("heartbeat", "metric", "broker_snapshot")


def connect(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")  # the observability view can never write
    return c


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(IST)


def _age(ts: str, now: datetime) -> float:
    t = datetime.fromisoformat(ts)
    t = t if t.tzinfo else t.replace(tzinfo=IST)
    return max(0.0, (now - t).total_seconds())


def in_hours(now: datetime) -> bool:
    return now.weekday() < 5 and HOURS[0] <= now.timetz().replace(tzinfo=None) <= HOURS[1]


# ── live ──────────────────────────────────────────────────────────────────────


def live(db_path: str, now: datetime | None = None) -> dict:
    now = _now(now)
    day = now.date().isoformat()
    c = connect(db_path)
    try:
        session = _session(c, day)
        trades = _trades(c, day, now)
        components = _components(c, now)
        out = {
            "generated_at": now.isoformat(timespec="seconds"),
            "day": day,
            "session": session,
            "bot_mode": _bot_mode(c),
            "kpis": _kpis(trades),
            "trades": trades,
            "components": components,
            "attention": _attention(c, session, trades, components, now),
            "stream": _stream(c, day),
            "pnl": _pnl_series(c, day),
            "drift": {"available": False, "why": "no broker_snapshot events yet (OBS-2)"},
            "cursor": c.execute("SELECT COALESCE(MAX(event_id), 0) FROM events").fetchone()[0],
        }
    finally:
        c.close()
    return out


_VOLATILE = frozenset({"generated_at", "age_s", "state_age_s"})  # change every second


def fingerprint(snapshot: dict) -> str:
    """Changes when anything but the clock does: the WebSocket pushes on a new value
    (the browser ages the numbers itself between pushes)."""

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in _VOLATILE}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x

    return hashlib.sha1(
        json.dumps(strip(snapshot), sort_keys=True, default=str).encode()
    ).hexdigest()


def _session(c: sqlite3.Connection, day: str) -> dict | None:
    r = c.execute("SELECT * FROM trading_sessions WHERE session_date=?", (day,)).fetchone()
    if r is None:
        return None
    detail = json.loads(r["halt_detail"]) if "halt_detail" in r.keys() and r["halt_detail"] else {}  # noqa: SIM118
    return {
        "state": r["state"],
        "mode": r["mode"],
        "halt_reason": r["halt_reason"],
        "halt_acked": bool(r["halt_acked_at"]),
        "halt_detail": detail,
        "updated_at": r["updated_at"],
    }


def _bot_mode(c: sqlite3.Connection) -> str:
    r = c.execute("SELECT mode FROM bot_mode WHERE id=1").fetchone()
    return r["mode"] if r else "auto"


def _trades(c: sqlite3.Connection, day: str, now: datetime) -> list[dict]:
    rows = c.execute(
        "SELECT b.*, p.current_price, p.unrealised_pnl, p.unprotected_flag, p.stop_loss_price"
        " FROM intraday_brackets b LEFT JOIN positions p ON p.position_id=b.position_id"
        " WHERE b.session_date=? ORDER BY b.created_at",
        (day,),
    ).fetchall()
    out = []
    for r in rows:
        live_state = r["state"] in ("protecting", "open", "exiting")
        stop = r["stop_loss_price"] if r["stop_loss_price"] else r["stop_price"]
        out.append(
            {
                "id": r["bracket_id"],
                "symbol": r["symbol"],
                "state": r["state"],
                "quantity": r["filled_quantity"] or r["planned_quantity"],
                "entry": r["entry_price"],
                "stop": stop,
                "target": r["target_price"],
                "last": r["current_price"] if live_state else None,
                "unrealised": r["unrealised_pnl"] if live_state else None,
                "realised": r["realised_pnl"],
                "exit_reason": r["exit_reason"],
                "protected": r["state"] == "open" and not r["unprotected_flag"],
                "unprotected": bool(r["unprotected_flag"]),
                "state_age_s": _age(r["updated_at"], now) if r["updated_at"] else None,
                "created_at": r["created_at"],
                "closed_at": r["closed_at"],
            }
        )
    return out


def _kpis(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["state"] == "closed"]
    active = [t for t in trades if t["state"] in ("entry_pending", "protecting", "open", "exiting")]
    realised = sum(float(t["realised"] or 0) for t in closed)
    return {
        "realised": round(realised, 2),
        "unrealised": round(sum(float(t["unrealised"] or 0) for t in active), 2),
        "active": len(active),
        "closed": len(closed),
        "wins": sum(1 for t in closed if (t["realised"] or 0) > 0),
        "losses": sum(1 for t in closed if (t["realised"] or 0) < 0),
        "forced_exits": sum(
            1
            for t in closed
            if (t["exit_reason"] or "").startswith(
                ("stop_not", "stop_rej", "stop_fail", "stop_canc", "stop_lost")
            )
        ),
    }


def _components(c: sqlite3.Connection, now: datetime) -> list[dict]:
    """Liveness per source, from the newest heartbeat of the newest boot."""
    rows = c.execute(
        "SELECT source, MAX(event_id) AS last_id FROM events WHERE kind='heartbeat'"
        " GROUP BY source ORDER BY source"
    ).fetchall()
    out = []
    for r in rows:
        hb = c.execute("SELECT * FROM events WHERE event_id=?", (r["last_id"],)).fetchone()
        p = describe.payload(hb)
        age = _age(hb["ts"], now)
        status = "alive"
        if age > SILENT_AFTER_SECONDS:
            status = "silent" if in_hours(now) else "idle"
        started = c.execute(
            "SELECT ts FROM events WHERE source=? AND boot_id=? AND kind='lifecycle'"
            " AND name='started' ORDER BY event_id LIMIT 1",
            (hb["source"], hb["boot_id"]),
        ).fetchone()
        out.append(
            {
                "source": hb["source"],
                "status": status,
                "age_s": round(age),
                "last_seen": hb["ts"],
                "started": started["ts"] if started else None,
                "queue_depth": p.get("queue_depth"),
                "dropped": p.get("dropped"),
                "health": {k: v for k, v in p.items() if k not in ("queue_depth", "dropped")},
                "seq_gap": _seq_gap(c, hb["source"], hb["boot_id"]),
            }
        )
    return out


def _seq_gap(c: sqlite3.Connection, source: str, boot_id: str) -> int:
    """Events lost within one boot: the sequence should be gapless (§4)."""
    r = c.execute(
        "SELECT COUNT(*), MIN(source_seq), MAX(source_seq) FROM events"
        " WHERE source=? AND boot_id=?",
        (source, boot_id),
    ).fetchone()
    n, lo, hi = r[0], r[1], r[2]
    return int(hi - lo + 1 - n) if n else 0


def _attention(c, session, trades, components, now) -> list[dict]:
    items: list[dict] = []
    if session and session["state"] == "halted":
        why = session["halt_reason"] or "unknown"
        items.append(_item("critical", f"Session halted: {why}", "session", None))
    for t in trades:
        if t["unprotected"]:
            items.append(
                _item("critical", f"{t['symbol']} has no confirmed stop", "trade", t["id"])
            )
        elif t["state"] == "protecting" and (t["state_age_s"] or 0) > PROTECTING_TOO_LONG_SECONDS:
            items.append(_item("critical", f"{t['symbol']} stuck in protecting", "trade", t["id"]))
        elif t["state"] == "exiting" and (t["state_age_s"] or 0) > PROTECTING_TOO_LONG_SECONDS:
            items.append(_item("warn", f"{t['symbol']} exit is taking long", "trade", t["id"]))
    for comp in components:
        if comp["status"] == "silent":
            items.append(
                _item(
                    "warn",
                    f"{comp['source']} silent for {comp['age_s']}s",
                    "component",
                    comp["source"],
                )
            )
        if comp["seq_gap"]:
            items.append(
                _item(
                    "warn",
                    f"{comp['source']} lost {comp['seq_gap']} events",
                    "component",
                    comp["source"],
                )
            )
        if comp["dropped"]:
            items.append(
                _item(
                    "warn",
                    f"{comp['source']} dropped {comp['dropped']} events",
                    "component",
                    comp["source"],
                )
            )
    since = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    for r in c.execute(
        "SELECT * FROM events WHERE severity='critical' AND ts>=? AND kind!='heartbeat'"
        " ORDER BY event_id DESC LIMIT 5",
        (since,),
    ):
        items.append(_item("critical", describe.title(r), "event", r["trade_id"], ts=r["ts"]))
    order = {"critical": 0, "warn": 1}
    return sorted(items, key=lambda i: order.get(i["severity"], 2))[:12]


def _item(severity: str, text: str, kind: str, ref: str | None, ts: str | None = None) -> dict:
    return {"severity": severity, "text": text, "kind": kind, "ref": ref, "ts": ts}


def _stream(c: sqlite3.Connection, day: str) -> list[dict]:
    marks = ",".join("?" * len(_ROUTINE_KINDS))
    rows = c.execute(
        f"SELECT * FROM events WHERE session_date=? AND kind NOT IN ({marks})"  # noqa: S608
        " AND NOT (entity_type='order' AND name IN ('created','submitting','pending','filled'))"
        " ORDER BY event_id DESC LIMIT ?",  # routine order steps live in the trade timeline
        (day, *_ROUTINE_KINDS, STREAM_LIMIT),
    ).fetchall()
    return [describe.summary(r) for r in rows]


def _pnl_series(c: sqlite3.Connection, day: str) -> list[dict]:
    """Cumulative realised P&L, one point per closed trade, from the `flat` events."""
    total, out = 0.0, []
    for r in c.execute(
        "SELECT ts, symbol, payload FROM events WHERE session_date=? AND entity_type='trade'"
        " AND kind='state_transition' AND name='flat' ORDER BY event_id",
        (day,),
    ):
        pnl = float((describe.payload(r).get("inputs") or {}).get("realised_pnl") or 0)
        total += pnl
        out.append(
            {"ts": r["ts"], "symbol": r["symbol"], "pnl": round(pnl, 2), "cum": round(total, 2)}
        )
    return out


# ── timelines ─────────────────────────────────────────────────────────────────


def trade_timeline(db_path: str, trade_id: str) -> dict | None:
    c = connect(db_path)
    try:
        b = c.execute("SELECT * FROM intraday_brackets WHERE bracket_id=?", (trade_id,)).fetchone()
        if b is None:
            return None
        order_ids = [b["entry_order_id"], b["stop_order_id"], b["target_order_id"]]
        order_ids += json.loads(b["exit_order_ids"] or "[]")
        order_ids = [o for o in order_ids if o]
        marks = ",".join("?" * len(order_ids)) or "NULL"
        rows = c.execute(
            f"SELECT * FROM events WHERE (trade_id=? OR (entity_type='trade' AND entity_id=?)"  # noqa: S608
            f" OR order_id IN ({marks})) AND kind NOT IN ('heartbeat','metric')"
            " ORDER BY event_id LIMIT ?",
            (trade_id, trade_id, *order_ids, TIMELINE_LIMIT),
        ).fetchall()
        orders = {
            r["order_id"]: dict(r)
            for r in c.execute(
                f"SELECT order_id, side, order_type, status, quantity, filled_quantity,"  # noqa: S608
                f" average_fill_price FROM orders WHERE order_id IN ({marks})",
                order_ids,
            )
        }
    finally:
        c.close()
    return {
        "trade": {
            k: b[k]
            for k in (
                "bracket_id",
                "symbol",
                "state",
                "planned_quantity",
                "filled_quantity",
                "entry_price",
                "stop_price",
                "target_price",
                "exit_reason",
                "realised_pnl",
                "session_date",
            )
        },  # fmt: skip
        "orders": list(orders.values()),
        "events": [describe.full(r) for r in rows],
    }


def session_timeline(db_path: str, day: str) -> dict:
    c = connect(db_path)
    try:
        sess = _session(c, day)
        trades = _trades(c, day, datetime.now(IST))
        marks = ",".join("?" * len(_ROUTINE_KINDS))
        rows = c.execute(
            f"SELECT * FROM events WHERE session_date=? AND kind NOT IN ({marks})"  # noqa: S608
            " AND (entity_type IN ('session','trade') OR kind IN ('decision','rejection','error'))"
            " ORDER BY event_id LIMIT ?",
            (day, *_ROUTINE_KINDS, TIMELINE_LIMIT),
        ).fetchall()
    finally:
        c.close()
    return {
        "day": day,
        "session": sess,
        "trades": trades,
        "events": [describe.full(r) for r in rows],
    }


def days(db_path: str, n: int = 14) -> list[dict]:
    """Daily results, newest first: realised P&L and trade counts per session."""
    c = connect(db_path)
    try:
        rows = c.execute(
            "SELECT b.session_date AS day, s.mode AS mode, s.state AS state,"
            " COUNT(*) AS trades, SUM(b.state='closed') AS closed,"
            " COALESCE(SUM(CASE WHEN b.state='closed' THEN b.realised_pnl END), 0) AS realised,"
            " COALESCE(SUM(b.state='closed' AND b.realised_pnl>0), 0) AS wins,"
            " COALESCE(SUM(b.state='closed' AND b.realised_pnl<0), 0) AS losses"
            " FROM intraday_brackets b"
            " LEFT JOIN trading_sessions s ON s.session_date=b.session_date"
            " GROUP BY b.session_date ORDER BY b.session_date DESC LIMIT ?",
            (max(1, min(n, 90)),),
        ).fetchall()
    finally:
        c.close()
    return [dict(r) | {"realised": round(r["realised"], 2)} for r in rows]
