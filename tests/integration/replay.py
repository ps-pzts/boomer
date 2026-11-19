"""Event-log helpers for #88: a normalised log (equal across identical runs) and a
replay of state_transition events that must rebuild every machine row's state."""

from __future__ import annotations

import json
import re
import sqlite3

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DROP = {"ts", "boot_id", "session_date", "event_id"}  # wall-clock and per-process noise
_TABLES = {
    "order": ("orders", "order_id", "status"),
    "trade": ("intraday_brackets", "bracket_id", "state"),
    "session": ("trading_sessions", "session_date", "state"),
}


def normalised_log(db: sqlite3.Connection) -> list[dict]:
    """Every event in order with UUIDs numbered by first appearance and clocks removed."""
    seen: dict[str, str] = {}

    def number(text: str) -> str:
        return _UUID.sub(lambda m: seen.setdefault(m.group(0), f"id{len(seen)}"), text)

    rows = db.execute("SELECT * FROM events ORDER BY event_id").fetchall()
    first_seq: dict[str, int] = {}  # source_seq counts per process, not per run: rebase it
    out = []
    for r in rows:
        rec = {k: r[k] for k in r.keys() if k not in _DROP}  # noqa: SIM118 (sqlite3.Row)
        rec["source_seq"] = r["source_seq"] - first_seq.setdefault(r["source"], r["source_seq"])
        out.append(json.loads(number(json.dumps(rec, sort_keys=True, default=str))))
    return out


def replayed_states(db: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Fold each entity's state_transition events: the last `to_state` is its state."""
    states: dict[tuple[str, str], str] = {}
    for r in db.execute(
        "SELECT entity_type, entity_id, payload FROM events"
        " WHERE kind='state_transition' ORDER BY event_id"
    ):
        p = json.loads(r["payload"])
        states[(r["entity_type"], r["entity_id"])] = p["to_state"]
    return states


def table_states(db: sqlite3.Connection) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for machine, (table, idcol, col) in _TABLES.items():
        for r in db.execute(f"SELECT {idcol}, {col} FROM {table}"):  # noqa: S608
            out[(machine, str(r[0]))] = r[1]
    return out


def replay_mismatches(db: sqlite3.Connection) -> list[str]:
    replay, tables = replayed_states(db), table_states(db)
    out = [
        f"{k}: table={tables[k]!r} replay={replay.get(k)!r}"
        for k in tables
        if tables[k] != replay.get(k)
    ]
    out += [f"{k}: replay has {v!r} but no row" for k, v in replay.items() if k not in tables]
    return out
