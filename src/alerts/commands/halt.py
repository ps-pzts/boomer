"""/halt and /ack: understand a halted session and clear a reconcile mismatch (#103)."""

from __future__ import annotations

import json

from alerts.commands.common import conn, now_ist, session_row
from alerts.format import esc, header, note, stamp
from executor.session_halt import acknowledge_mismatch
from executor.session_machine import KITE_CHECK_HALTS, RESUMABLE

_ADVICE = {
    "reconcile_mismatch": "Fix it at the broker, then /ack. The next reconcile may then resume.",
    "feed_stale": "Resumes by itself once ticks are fresh and a reconcile is clean.",
    "paused": "Send /resume.",
    "breaker:nifty_move": "Resumes by itself when the index move is back inside the limit.",
}


def halt(db_path: str, args: str = "") -> str:
    c = conn(db_path)
    try:
        row = session_row(c)
    finally:
        c.close()
    if row is None:
        return "⚪ No session today yet."
    if row["state"] != "halted":
        return f"🟢 Not halted: session is <b>{esc(row['state'])}</b>."
    reason = row["halt_reason"] or "unknown"
    if reason in KITE_CHECK_HALTS:
        advice = "Fix the Kite problem, then /canary (resumes only if both checks pass)."
    elif reason in RESUMABLE:
        advice = _ADVICE.get(reason, "Resumes after a clean reconcile once the cause clears.")
    else:
        advice = "Not resumable today: no new entries; open trades stay managed."
    lines = [header("🛑", f"Halted: {reason}", stamp()), note(esc(advice))]
    detail = json.loads(row["halt_detail"]) if row["halt_detail"] else {}
    failed = detail.get("failed_checks") or []
    if failed:
        lines.append("<b>Failed reconcile checks</b>")
        for name in failed:
            info = {k: v for k, v in (detail.get(name) or {}).items() if k != "ok"}
            lines.append(f"🔴 {esc(name)}: <code>{esc(json.dumps(info, default=str)[:300])}</code>")
    elif detail:
        lines.append(
            "<b>Inputs</b> <code>" + esc(json.dumps(detail, default=str)[:400]) + "</code>"
        )
    if reason == "reconcile_mismatch":
        lines.append("Acknowledged: " + ("🟢 yes" if row["halt_acked_at"] else "🔴 not yet"))
    return "\n".join(lines)


def ack(db_path: str, args: str = "") -> str:
    c = conn(db_path)
    try:
        row = session_row(c)
        if row is None or row["state"] != "halted" or row["halt_reason"] != "reconcile_mismatch":
            what = (
                "no session"
                if row is None
                else f"{row['state']}, {row['halt_reason'] or 'no halt'}"
            )
            return f"⛔ <b>Nothing to acknowledge</b> ({esc(what)})\n" + note(
                "/ack only applies to a reconcile mismatch."
            )
        done = acknowledge_mismatch(c, now_ist().date(), "telegram", now_ist())
    finally:
        c.close()
    if not done:
        return "⛔ <b>Nothing to acknowledge</b>"
    return "✅ <b>Acknowledged</b>\n" + note(
        "The next reconcile may resume trading if it comes back clean."
    )
