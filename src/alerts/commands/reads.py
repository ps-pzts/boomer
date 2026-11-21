"""/positions, /trades, /pnl: read-only views of today's book (#103)."""

from __future__ import annotations

from alerts.commands.common import conn, today
from alerts.format import capped, dot, esc, header, money, rows, stamp

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
        rows_ = c.execute(
            "SELECT symbol, quantity, average_entry_price, current_price, unrealised_pnl,"
            " stop_loss_price, target_price, unprotected_flag FROM positions"
            " WHERE is_open=1 AND track='intraday' ORDER BY entry_at"
        ).fetchall()
    finally:
        c.close()
    head = header("📈", "Open positions" + (f" ({len(rows_)})" if rows_ else ""), stamp())
    if not rows_:
        return head + "\nNone."
    return (
        head
        + "\n"
        + capped(
            [
                f"{dot(r['unrealised_pnl'] >= 0)} <b>{esc(r['symbol'])}</b> {r['quantity']} @ "
                f"{r['average_entry_price']:,.2f}→{r['current_price']:,.2f} "
                f"{money(r['unrealised_pnl'])}"
                f" · 🛑 {r['stop_loss_price']:,.2f} 🎯 {r['target_price']:,.2f}"
                + ("  🚨 <b>UNPROTECTED</b>" if r["unprotected_flag"] else "")
                for r in rows_
            ]
        )
    )


def trades(db_path: str, args: str = "") -> str:
    c = conn(db_path)
    try:
        rows_ = c.execute(
            "SELECT symbol, state, planned_quantity, filled_quantity, entry_price, stop_price,"
            " target_price, exit_reason, realised_pnl FROM intraday_brackets"
            " WHERE session_date=? ORDER BY created_at",
            (today(),),
        ).fetchall()
    finally:
        c.close()
    head = header("🧾", "Today's trades" + (f" ({len(rows_)})" if rows_ else ""), stamp())
    if not rows_:
        return head + "\nNone yet."
    items = []
    for r in rows_:
        tail = ""
        if r["state"] in ("closed", "cancelled"):
            tail = f" — {esc(r['exit_reason'] or r['state'])}"
            if r["realised_pnl"] is not None:
                tail += f", {money(r['realised_pnl'])}"
        items.append(
            f"{_STATE_ICON.get(r['state'], '•')} <b>{esc(r['symbol'])}</b> "
            f"{r['filled_quantity'] or r['planned_quantity']} {esc(r['state'])}{tail}\n"
            f"   entry {r['entry_price']:,.2f} · stop {r['stop_price']:,.2f}"
            f" · target {r['target_price']:,.2f}"
        )
    return head + "\n" + capped(items, 12)


def pnl_numbers(db_path: str, day: str | None = None) -> dict:
    c = conn(db_path)
    try:
        closed = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(realised_pnl), 0),"
            " COALESCE(SUM(realised_pnl > 0), 0), COALESCE(SUM(realised_pnl < 0), 0)"
            " FROM intraday_brackets WHERE session_date=? AND state='closed'",
            (day or today(),),
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
    total = n["realised"] + n["unrealised"]
    return (
        header("💰", "P&L today", stamp())
        + f"\n{dot(total >= 0)} <b>{money(total)}</b> total\n"
        + rows(
            [
                ("Realised", money(n["realised"])),
                ("Closed", f"{n['closed']} ({n['wins']} won, {n['losses']} lost)"),
                ("Unrealised", money(n["unrealised"])),
                ("Open", n["open"]),
            ]
        )
    )
