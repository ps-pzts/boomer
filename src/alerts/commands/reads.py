"""/positions, /trades, /pnl: read-only views of today's book (#103)."""

from __future__ import annotations

from src.alerts.commands.common import conn, esc, money, today

_STATE_ICON = {
    "entry_pending": "⏳",
    "protecting": "🛡️",
    "open": "🟢",
    "exiting": "🚪",
    "closed": "⚪",
    "cancelled": "✖️",
}


def positions(db_path: str, args: str = "") -> str:
    c = conn(db_path)
    try:
        rows = c.execute(
            "SELECT symbol, quantity, average_entry_price, current_price, unrealised_pnl,"
            " stop_loss_price, target_price, unprotected_flag FROM positions"
            " WHERE is_open=1 AND track='intraday' ORDER BY entry_at"
        ).fetchall()
    finally:
        c.close()
    if not rows:
        return "<b>Open positions</b>\nNone."
    lines = ["<b>Open positions</b>"]
    for r in rows:
        flag = "  🚨 <b>UNPROTECTED</b>" if r["unprotected_flag"] else ""
        lines.append(
            f"<b>{esc(r['symbol'])}</b> {r['quantity']} @ {r['average_entry_price']:,.2f}"
            f" (now {r['current_price']:,.2f}, {money(r['unrealised_pnl'])})\n"
            f"  🛑 stop {r['stop_loss_price']:,.2f}  🎯 target {r['target_price']:,.2f}{flag}"
        )
    return "\n".join(lines)


def trades(db_path: str, args: str = "") -> str:
    c = conn(db_path)
    try:
        rows = c.execute(
            "SELECT symbol, state, planned_quantity, filled_quantity, entry_price, stop_price,"
            " target_price, exit_reason, realised_pnl FROM intraday_brackets"
            " WHERE session_date=? ORDER BY created_at",
            (today(),),
        ).fetchall()
    finally:
        c.close()
    if not rows:
        return "<b>Today's trades</b>\nNone yet."
    lines = [f"<b>Today's trades</b> ({len(rows)})"]
    for r in rows:
        qty = r["filled_quantity"] or r["planned_quantity"]
        tail = ""
        if r["state"] in ("closed", "cancelled"):
            tail = f" — {esc(r['exit_reason'] or r['state'])}"
            if r["realised_pnl"] is not None:
                tail += f", {money(r['realised_pnl'])}"
        lines.append(
            f"{_STATE_ICON.get(r['state'], '•')} <b>{esc(r['symbol'])}</b> {qty} "
            f"{esc(r['state'])}: entry {r['entry_price']:,.2f}, stop {r['stop_price']:,.2f}, "
            f"target {r['target_price']:,.2f}{tail}"
        )
    return "\n".join(lines)


def pnl_numbers(db_path: str) -> dict:
    c = conn(db_path)
    try:
        closed = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(realised_pnl), 0),"
            " COALESCE(SUM(realised_pnl > 0), 0), COALESCE(SUM(realised_pnl < 0), 0)"
            " FROM intraday_brackets WHERE session_date=? AND state='closed'",
            (today(),),
        ).fetchone()
        open_row = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(unrealised_pnl), 0) FROM positions"
            " WHERE is_open=1 AND track='intraday'"
        ).fetchone()
    finally:
        c.close()
    return {
        "closed": closed[0],
        "realised": float(closed[1]),
        "wins": int(closed[2]),
        "losses": int(closed[3]),
        "open": open_row[0],
        "unrealised": float(open_row[1]),
    }


def pnl(db_path: str, args: str = "") -> str:
    n = pnl_numbers(db_path)
    return (
        "<b>P&amp;L today</b>\n"
        f"Realised: {money(n['realised'])} over {n['closed']} closed "
        f"({n['wins']} won, {n['losses']} lost)\n"
        f"Unrealised: {money(n['unrealised'])} on {n['open']} open\n"
        f"Total: {money(n['realised'] + n['unrealised'])}"
    )
