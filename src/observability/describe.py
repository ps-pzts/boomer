"""Plain-text one-liners for events (#105): what the live stream and timelines show.

Pure functions of an events row. Plain text only: the browser inserts it with
`textContent`, never as HTML, because symbols, rules and reasons are data.
"""

from __future__ import annotations

import json
from typing import Any

# Stream filter groups the UI offers. An event belongs to exactly one.
GROUPS = ("trade", "session", "alert", "other")

_TRADE = {
    "created": "Entry placed",
    "entry_filled": "Entry filled",
    "stop_confirmed": "Stop confirmed at the broker",
    "stop_raised": "Stop raised",
    "leg_filled": "Exit leg filled",
    "flat": "Trade closed",
    "stop_failed": "Stop could not be placed",
    "stop_lost": "Stop lost",
    "exit_failed": "Exit order failed",
    "corrective_order": "Corrective order",
    "square_off": "Square-off",
    "entry_expired": "Entry expired",
    "entry_rejected": "Entry rejected",
}
_SESSION = {
    "created": "Session created",
    "setup_ok": "Feed connected",
    "watchlist_ready": "Watchlist ready",
    "market_open": "Market open: trading",
    "halt": "Session halted",
    "checks_failed": "Kite checks failed: halted",
    "checks_passed": "Kite checks passed",
    "recover": "Recovering",
    "reconciled": "Reconciled: trading",
    "reconcile_failed": "Reconcile failed: halted",
    "restarted": "Process restarted",
    "entry_cutoff": "No new entries",
    "square_off": "Squaring off",
    "all_flat": "Flat: session closed",
    "abandon": "Abandoned (never traded)",
}


def payload(row: Any) -> dict:
    try:
        return json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}


def group(row: Any) -> str:
    if row["severity"] in ("warn", "critical") or row["kind"] in ("error", "alert_sent"):
        return "alert"
    return {"trade": "trade", "session": "session"}.get(row["entity_type"], "other")


def title(row: Any) -> str:
    kind, name, et = row["kind"], row["name"], row["entity_type"]
    if kind == "state_transition":
        if et == "trade":
            return _TRADE.get(name, name.replace("_", " "))
        if et == "session":
            return _SESSION.get(name, name.replace("_", " "))
        return f"Order {name}"
    if kind == "transition_blocked":
        return f"{et or 'machine'} blocked: {name.replace('_', ' ')}"
    if kind in ("decision", "rejection"):
        return name.replace("_", " ").capitalize()
    if kind == "lifecycle":
        return f"{row['entity_id'] or row['source']} {name}"
    if kind == "error":
        return f"Error in {payload(row).get('where') or row['source']}"
    return f"{kind}: {name.replace('_', ' ')}"


def detail(row: Any) -> str:
    """The rule and the headline numbers, in one short line."""
    p = payload(row)
    bits: list[str] = []
    rule = p.get("rule") or (p.get("blocked_by") if row["kind"] == "transition_blocked" else None)
    if rule:
        bits.append(f"rule {rule}")
    inputs = p.get("inputs") or {}
    for key in (
        "qty",
        "quantity",
        "price",
        "stop",
        "target",
        "realised_pnl",
        "net",
        "filled_quantity",
    ):
        if key in inputs and inputs[key] is not None:
            bits.append(f"{key} {inputs[key]}")
    if row["kind"] == "error" and p.get("exception"):
        bits.append(str(p["exception"])[:120])
    return ", ".join(bits)


def summary(row: Any) -> dict:
    """The compact event the UI lists."""
    return {
        "id": row["event_id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "name": row["name"],
        "severity": row["severity"],
        "group": group(row),
        "symbol": row["symbol"],
        "trade_id": row["trade_id"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "title": title(row),
        "detail": detail(row),
    }


def full(row: Any) -> dict:
    """A timeline entry: the summary plus the whole payload for the expandable detail."""
    out = summary(row)
    p = payload(row)
    out["from_state"] = p.get("from_state")
    out["to_state"] = p.get("to_state")
    out["payload"] = p
    out["source"] = row["source"]
    return out
