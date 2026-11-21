"""/status: the session at a glance (#103). Replaces the pre-redesign snapshot that still
reported recommendations and queued orders."""

from __future__ import annotations

from alerts.commands.common import conn, session_row, today
from alerts.commands.reads import pnl_numbers
from alerts.format import dot, esc, header, money, note, rows, stamp

_STATE_ICON = {
    "trading": "🟢",
    "halted": "🛑",
    "reconciling": "🔄",
    "no_new_entries": "🟡",
    "squaring_off": "🚪",
    "closed": "⚪",
    "abandoned": "⚪",
}
_MODE_ICON = {"auto": "🟢", "paused": "🟡", "emergency_stop": "🔴"}


def status(db_path: str, args: str = "") -> str:
    day = today()
    c = conn(db_path)
    try:
        sess = session_row(c, day)
        bot = c.execute("SELECT mode FROM bot_mode WHERE id=1").fetchone()
        checks = c.execute(
            "SELECT check_name, ok, ran_at FROM kite_daily_checks WHERE id IN"
            " (SELECT MAX(id) FROM kite_daily_checks WHERE session_date=? GROUP BY check_name)",
            (day,),
        ).fetchall()
        active = c.execute(
            "SELECT COUNT(*) FROM intraday_brackets WHERE state NOT IN ('closed','cancelled')"
        ).fetchone()[0]
    finally:
        c.close()
    bot_mode = bot["mode"] if bot else "auto"
    n = pnl_numbers(db_path)
    out = [header("📊", "Boomer", stamp(sess["mode"] if sess else None))]
    if sess is None:
        out.append("⚪ Session not started today")
    else:
        out.append(f"{_STATE_ICON.get(sess['state'], '⚪')} Session <b>{esc(sess['state'])}</b>")
        if sess["halt_reason"]:
            out.append(note(f"halted: {esc(sess['halt_reason'])} → /halt"))
    out.append(f"{_MODE_ICON.get(bot_mode, '⚪')} Bot <b>{esc(bot_mode)}</b>")
    out.append(
        "Kite checks: "
        + (
            "  ".join(
                f"{dot(bool(r['ok']))} {esc(r['check_name'])} {esc(r['ran_at'][11:16])}"
                for r in checks
            )
            or "none run today"
        )
    )
    out.append(
        rows(
            [
                ("Active", active),
                ("Closed", f"{n['closed']} ({n['wins']}W/{n['losses']}L)"),
                ("Realised", money(n["realised"])),
                ("Unrealised", money(n["unrealised"])),
            ]
        )
    )
    return "\n".join(out)
