"""Live Telegram feed of trade and session events, plus the end-of-day summary (#103).

A separate process (`python -m observability.telegram_feed`) that tails the `events`
table. Only src/observability may read events (designs/observability.md §2), and it only
reads: nothing here can influence a trading decision. It remembers the last event it sent
in `telegram_feed_cursor`, so a restart neither repeats nor skips messages; a first run
starts at "now" instead of replaying history.

Messages (one per event; order-machine noise is left out):
  📥 entry placed   🟢 filled   🛡️ protected   🎯/🛑 target or stop hit   ⚪ closed + P&L
  ✖️ cancelled   🚨 unprotected / stop lost   🛑 session halted   ✅ resumed   🏁 day closed
`BOOMER_TELEGRAM_FEED=off` mutes it (the commands keep working).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from alerts.commands.common import esc, money
from alerts.commands.reads import pnl_numbers
from alerts.telegram import send_telegram

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
POLL_SECONDS = 5
SEND_GAP_SECONDS = 0.4  # Telegram allows about one message a second per chat
BATCH = 50


def _inputs(p: dict) -> dict:
    return p.get("inputs") or {}


def render(row: sqlite3.Row) -> str | None:
    """The message for one event row, or None when it is not worth a message."""
    if row["kind"] != "state_transition":
        return None
    p = json.loads(row["payload"] or "{}")
    name, rule, inputs = row["name"], p.get("rule", ""), _inputs(p)
    sym = esc(row["symbol"] or "")
    if row["entity_type"] == "trade":
        return _trade(name, rule, inputs, sym)
    if row["entity_type"] == "session":
        return _session(name, rule, inputs, p)
    if row["entity_type"] == "order" and name == "rejected":
        return f"⚠️ <b>{sym}</b> order rejected by the broker ({esc(rule)})"
    return None


def _trade(name: str, rule: str, inputs: dict, sym: str) -> str | None:
    if name == "created":
        return (
            f"📥 <b>{sym}</b> entry placed: {inputs.get('quantity')} @ "
            f"{float(inputs.get('entry_price') or 0):,.2f} "
            f"(stop {float(inputs.get('stop_price') or 0):,.2f}, "
            f"target {float(inputs.get('target_price') or 0):,.2f})"
        )
    if name == "entry_filled":
        return f"🟢 <b>{sym}</b> entry filled: {inputs.get('qty')} @ {inputs.get('price')}"
    if name == "stop_confirmed":
        return f"🛡️ <b>{sym}</b> protected: stop {inputs.get('stop')}, target {inputs.get('target')}"
    if name == "stop_raised":
        return f"⬆️ <b>{sym}</b> stop raised to {inputs.get('new_stop') or inputs.get('stop')}"
    if name == "leg_filled":
        icon, what = ("🎯", "target hit") if rule == "target_hit" else ("🛑", "stop hit")
        return f"{icon} <b>{sym}</b> {what}"
    if name == "flat":
        pnl = float(inputs.get("realised_pnl") or 0)
        return f"⚪ <b>{sym}</b> closed: {money(pnl)}"
    if name in ("stop_failed", "stop_lost"):
        return f"🚨 <b>{sym}</b> UNPROTECTED ({esc(rule)}): exiting at market"
    if name == "exit_failed":
        return f"🚨 <b>{sym}</b> exit order failed ({esc(rule)}): retrying"
    if name == "square_off":
        return f"🚪 <b>{sym}</b> squaring off"
    if name in ("entry_expired", "entry_rejected"):
        return f"✖️ <b>{sym}</b> entry cancelled: {name.removeprefix('entry_')}"
    return None


def _session(name: str, rule: str, inputs: dict, p: dict) -> str | None:
    mode = ""
    if name in ("halt", "checks_failed"):
        return f"🛑 <b>Session HALTED</b>: {esc(rule)}  → /halt"
    if name == "reconcile_failed":
        return "🛑 <b>Session HALTED</b>: reconcile mismatch  → /halt, then /ack"
    if name in ("recover", "checks_passed", "reconciled") and p.get("to_state") == "trading":
        return f"✅ <b>Session resumed</b> (trading){mode}"
    if name == "market_open":
        return "🔔 <b>Market open</b>: trading"
    if name == "entry_cutoff":
        return "🟡 <b>No new entries</b> for the rest of the day"
    if name == "square_off":
        why = "EMERGENCY STOP" if rule == "emergency_stop" else "15:14 square-off"
        return f"🚪 <b>Session squaring off</b> ({why})"
    if name == "all_flat":
        return "🏁 <b>Session closed</b>: flat"
    if name == "abandon":
        return f"⚪ <b>Session abandoned</b> (never traded): {esc(rule)}"
    return None


def eod_summary(db_path: str, day: str) -> str:
    """End-of-day text from the machines' own tables."""
    c = sqlite3.connect(db_path, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT symbol, state, exit_reason, realised_pnl FROM intraday_brackets"
            " WHERE session_date=? ORDER BY created_at",
            (day,),
        ).fetchall()
        sess = c.execute(
            "SELECT mode FROM trading_sessions WHERE session_date=?", (day,)
        ).fetchone()
        unprotected = c.execute(
            "SELECT COUNT(*) FROM intraday_brackets WHERE session_date=?"
            " AND exit_reason IN ('stop_not_submitted','stop_rejected','stop_failed',"
            "'stop_cancelled','stop_lost')",
            (day,),
        ).fetchone()[0]
    finally:
        c.close()
    n = pnl_numbers(db_path, day)
    mode = (sess["mode"] if sess else "?").upper()
    lines = [
        f"<b>🏁 End of day — {day} ({mode})</b>",
        f"Trades: {len(rows)} ({n['closed']} closed: {n['wins']} won, {n['losses']} lost)",
        f"Realised P&amp;L: <b>{money(n['realised'])}</b>",
    ]
    for r in rows:
        if r["state"] == "closed":
            lines.append(
                f"  {esc(r['symbol'])}: {esc(r['exit_reason'])}, {money(r['realised_pnl'])}"
            )
        elif r["state"] == "cancelled":
            lines.append(f"  {esc(r['symbol'])}: cancelled ({esc(r['exit_reason'])})")
    if unprotected:
        lines.append(f"🚨 Exits forced by a missing stop: {unprotected}")
    else:
        lines.append("Exits forced by a missing stop: 0")
    return "\n".join(lines)


# ── the tail loop ─────────────────────────────────────────────────────────────


def _cursor(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT last_event_id FROM telegram_feed_cursor WHERE id=1").fetchone()
    if row:
        return int(row[0])
    start = c.execute("SELECT COALESCE(MAX(event_id), 0) FROM events").fetchone()[0]
    _save(c, int(start))
    return int(start)


def _save(c: sqlite3.Connection, event_id: int) -> None:
    now = datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
    c.execute(
        "INSERT INTO telegram_feed_cursor (id, last_event_id, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET last_event_id=excluded.last_event_id,"
        " updated_at=excluded.updated_at",
        (event_id, now),
    )
    c.commit()


def poll_once(db_path: str, send) -> int:
    """Send the messages for events after the cursor; returns how many were sent.
    `send(text) -> bool`. The cursor advances only past events whose message went out, so
    a Telegram outage delays messages instead of dropping them."""
    c = sqlite3.connect(db_path, timeout=5)
    c.row_factory = sqlite3.Row
    sent = 0
    full = False
    try:
        cursor = _cursor(c)
        rows = c.execute(
            "SELECT * FROM events WHERE event_id>? AND kind='state_transition'"
            " ORDER BY event_id LIMIT ?",
            (cursor, BATCH),
        ).fetchall()
        for row in rows:
            text = render(row)
            if text is not None:
                if not send(text):
                    return sent  # retry this event on the next poll
                sent += 1
                if row["entity_type"] == "session" and row["name"] == "all_flat":
                    day = row["entity_id"] or row["session_date"]
                    if send(eod_summary(db_path, day)):
                        sent += 1
                if sent > 1:
                    time.sleep(SEND_GAP_SECONDS)
            _save(c, int(row["event_id"]))
        full = len(rows) == BATCH
    finally:
        c.close()
    return sent + poll_once(db_path, send) if full else sent


def run_forever() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    db_path = os.environ.get("BOOMER_DB_PATH", "/var/lib/boomer/boomer.db")
    if not token or not chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    if os.environ.get("BOOMER_TELEGRAM_FEED", "on").lower() == "off":
        raise SystemExit("telegram feed is muted (BOOMER_TELEGRAM_FEED=off)")

    def send(text: str) -> bool:
        return send_telegram(token, chat, text)

    logger.info("telegram_feed_starting")
    while True:
        try:
            poll_once(db_path, send)
        except Exception:
            logger.exception("telegram_feed_poll_failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever()
