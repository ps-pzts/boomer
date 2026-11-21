"""Telegram operator commands (#103). `run(db_path, text)` returns the reply, or None when
the text is not one of these commands (the bot keeps its own /mode and /canary handlers)."""

from __future__ import annotations

from collections.abc import Callable

from alerts.commands.control import emergency, pause, resume
from alerts.commands.halt import ack, halt
from alerts.commands.readiness import readiness
from alerts.commands.reads import pnl, positions, trades
from alerts.commands.status import status

HANDLERS: dict[str, Callable[[str, str], str]] = {
    "/status": status,
    "/halt": halt,
    "/ack": ack,
    "/pause": pause,
    "/resume": resume,
    "/emergency": emergency,
    "/positions": positions,
    "/trades": trades,
    "/pnl": pnl,
    "/readiness": readiness,
}

HELP = (
    "<b>🤖 Boomer commands</b>\n\n"
    "<b>Look</b>\n"
    "/status — session, bot mode, Kite checks, trades, P&amp;L\n"
    "/positions — open positions with stops and targets\n"
    "/trades — today's trades and their state\n"
    "/pnl — realised and unrealised P&amp;L today\n"
    "/readiness — paper-to-live criteria with numbers (evidence only)\n\n"
    "<b>Steer</b>\n"
    "/pause — halt new entries (open trades stay managed)\n"
    "/resume — back to auto\n"
    "/emergency — flatten everything (asks for confirm)\n"
    "/halt — why the session is halted, and how to resume\n"
    "/ack — acknowledge a reconcile mismatch after fixing it\n\n"
    "<b>Setup</b>\n"
    "/mode — show paper or live (/mode paper, /mode live confirm)\n"
    "/toggle — flip next session paper &lt;-&gt; live (to live needs confirm)\n"
    "/canary — re-run today's Kite pre-flight and order canary"
)


def parse(text: str) -> tuple[str, str]:
    """('/cmd', 'args') from 'text', dropping a @botname suffix."""
    head, _, rest = text.strip().partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def run(db_path: str, text: str) -> str | None:
    cmd, args = parse(text)
    handler = HANDLERS.get(cmd)
    return handler(db_path, args) if handler else None
