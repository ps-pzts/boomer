"""/status: the session at a glance (#103). Replaces the pre-redesign snapshot that still
reported recommendations and queued GTTs."""

from __future__ import annotations

from src.alerts.commands.common import conn, esc, money, now_ist, session_row, today
from src.alerts.commands.reads import pnl_numbers

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
    lines = [f"<b>📊 Boomer — {day}</b>"]
    if sess is None:
        lines.append("Session: not started today")
    else:
        icon = _STATE_ICON.get(sess["state"], "⚪")
        lines.append(f"{icon} Session: <b>{esc(sess['state'])}</b> ({esc(sess['mode']).upper()})")
        if sess["halt_reason"]:
            lines.append(f"   halted: {esc(sess['halt_reason'])}  → /halt")
    lines.append(f"{_MODE_ICON.get(bot_mode, '⚪')} Bot mode: <b>{esc(bot_mode)}</b>")
    if checks:
        parts = [
            f"{'✅' if r['ok'] else '⛔'} {esc(r['check_name'])} {esc(r['ran_at'][11:16])}"
            for r in checks
        ]
        lines.append("Kite checks: " + ", ".join(parts))
    else:
        lines.append("Kite checks: none run today")
    n = pnl_numbers(db_path)
    lines.append(f"Trades: {active} active, {n['closed']} closed ({n['wins']}W/{n['losses']}L)")
    lines.append(f"P&amp;L: {money(n['realised'])} realised, {money(n['unrealised'])} unrealised")
    lines.append(f"<i>{now_ist().strftime('%H:%M:%S IST')}</i>")
    return "\n".join(lines)
