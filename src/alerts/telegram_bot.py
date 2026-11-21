"""Telegram command bot for Boomer operator interface.

Commands (see alerts/commands/ and /help): /status /positions /trades /pnl /halt /ack
/pause /resume /emergency (#103), /mode (#76), /canary (#85).

Uses Telegram Bot API (long-polling getUpdates). No SDK; stdlib urllib only.
Run as a standalone process alongside the orchestrator.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from alerts import commands
from alerts.format import esc, finish, header, note, rows, stamp

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_API = "https://api.telegram.org/bot{token}/{method}"


# ── Low-level Telegram API calls ──────────────────────────────────────────────


def _call(token: str, method: str, payload: dict, timeout: int = 15) -> dict:
    url = _API.format(token=token, method=method)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.warning(
            "telegram_api_error method=%s status=%d body=%s", method, exc.code, body[:200]
        )
        return {"ok": False}
    except Exception as exc:
        logger.warning("telegram_call_failed method=%s error=%s", method, exc)
        return {"ok": False}


def _send(token: str, chat_id: str | int, text: str, reply_markup: dict | None = None) -> None:
    payload: dict = {"chat_id": chat_id, "text": finish(text), "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _call(token, "sendMessage", payload)


# ── Command handlers ───────────────────────────────────────────────────────────


def handle_mode(db_path: str, text: str) -> str:
    """/mode [paper|live [confirm]] (#76). Applies from the next session."""
    from executor.trading_mode import (
        ENV_VAR,
        LiveTradingAuthorisationStore,
        paper_record,
        request_mode,
        resolve_trading_mode,
        session_mode,
    )

    env = os.environ.get(ENV_VAR)
    today = datetime.now(IST).date()
    parts = text.split()
    if len(parts) == 1:
        nxt = resolve_trading_mode(env, LiveTradingAuthorisationStore(db_path)).mode.value
        rec = paper_record(db_path)
        return (
            header("⚙️", "Trading mode", stamp())
            + "\n"
            + rows(
                [
                    ("Today", session_mode(db_path, today, env).upper()),
                    ("Next session", nxt.upper()),
                    ("Paper sessions closed", rec["paper_sessions_closed"]),
                    ("Paper trades", rec["paper_trades"]),
                    ("Paper P&L", f"₹{rec['paper_realised_pnl']:,.2f}"),
                    ("Unprotected exits", rec["unprotected_exits"]),
                ]
            )
        )
    result = request_mode(
        db_path,
        parts[1],
        actor="telegram",
        reason=f"telegram {text}",
        env_value=env,
        confirmed=len(parts) > 2 and parts[2] == "confirm",
        today=today,
    )
    hint = (
        "\n" + note("Send <code>/mode live confirm</code> to proceed.")
        if "Repeat with" in result.message
        else ""
    )
    return ("✅ " if result.ok else "⛔ ") + esc(result.message) + hint


def handle_toggle(db_path: str, text: str) -> str:
    """/toggle [confirm] (#103): flip the NEXT session between paper and live.

    paper -> live needs `/toggle confirm` (real money); live -> paper is immediate. Same
    audited path as /mode, so it applies from the next session, never mid-day."""
    from executor.trading_mode import (
        ENV_VAR,
        LiveTradingAuthorisationStore,
        resolve_trading_mode,
    )

    nxt = resolve_trading_mode(os.environ.get(ENV_VAR), LiveTradingAuthorisationStore(db_path))
    target = "paper" if nxt.mode.value == "live" else "live"
    confirmed = text.split()[1:2] == ["confirm"]
    if target == "paper":
        return handle_mode(db_path, "/mode paper")
    if not confirmed:  # shows the paper record (or why live is refused) before anything changes
        reply = handle_mode(db_path, "/mode live")
        return reply.replace("/mode live confirm", "/toggle confirm")
    return handle_mode(db_path, "/mode live confirm")


def handle_canary(db_path: str) -> str:
    """/canary (#85): re-run today's Kite pre-flight and (09:15-15:20) order canary
    on the real Kite account. Safe at any time; both passing lets a session halted
    by a failed check resume (through a broker reconcile)."""
    from executor import kite_checks

    try:
        broker = kite_checks.kite_broker_from_env()
    except Exception as exc:
        return f"⛔ <b>Could not connect to Kite</b>: {esc(exc)}"
    if broker is None:
        return "⛔ <b>Kite not configured</b>\n" + note("Set KITE_API_KEY and KITE_ACCESS_TOKEN.")
    reports = kite_checks.rerun(db_path, broker)
    lines = [header("🔌", "Kite checks re-run", stamp())]
    for r in reports:
        if r.ok:
            lines.append(f"🟢 {esc(r.kind)} passed")
        else:
            lines.append(f"🔴 {esc(r.kind)} <b>FAILED</b>: " + esc("; ".join(r.failures())))
    if len(reports) == 1:
        why = "pre-flight failed" if not reports[0].ok else "only 09:15-15:20 on a trading day"
        lines.append(note(f"canary not run ({why})"))
    elif all(r.ok for r in reports):
        lines.append(note("Trading may resume after the next clean reconcile."))
    return "\n".join(lines)


# ── Polling loop ───────────────────────────────────────────────────────────────


class TelegramBot:
    def __init__(self, token: str, chat_id: str, db_path: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db_path = db_path
        self._offset = 0

    _POLL_SECONDS = 20  # Telegram long-poll hold duration

    def _get_updates(self) -> list[dict]:
        result = _call(
            self._token,
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": self._POLL_SECONDS,
                "allowed_updates": ["message"],
            },
            timeout=self._POLL_SECONDS + 10,  # urllib must exceed the Telegram hold duration
        )
        if not result.get("ok"):
            return []
        return result.get("result", [])

    def _dispatch(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        # Only respond to the configured chat (security gate)
        if str(msg["chat"]["id"]) != str(self._chat_id):
            logger.warning("ignoring message from unknown chat_id=%s", msg["chat"]["id"])
            return

        text = (msg.get("text") or "").strip().lower()
        chat = msg["chat"]["id"]
        if not text.startswith("/"):
            return
        cmd, _ = commands.parse(text)
        if cmd == "/mode":
            _send(self._token, chat, handle_mode(self._db_path, text))
        elif cmd == "/toggle":
            _send(self._token, chat, handle_toggle(self._db_path, text))
        elif cmd == "/canary":
            _send(self._token, chat, handle_canary(self._db_path))
        elif cmd in ("/help", "/start"):
            _send(self._token, chat, commands.HELP)
        else:
            try:
                reply = commands.run(self._db_path, text)
            except Exception as exc:
                logger.exception("command_failed cmd=%s", cmd)
                reply = f"⛔ <b>{esc(cmd)} failed</b>: {esc(exc)}"
            _send(self._token, chat, reply or "Unknown command. Send /help.")

    def run_forever(self) -> None:
        import signal as _signal

        def _shutdown(signum: int, frame: object) -> None:
            logger.info("telegram_bot_stopping signal=%d", signum)
            _send(
                self._token,
                self._chat_id,
                header("🔴", "Boomer bot going offline", stamp())
                + "\n"
                + note(
                    f"Graceful shutdown (signal {signum}). "
                    "Commands will not be processed until restarted."
                ),
            )
            raise SystemExit(0)

        _signal.signal(_signal.SIGTERM, _shutdown)
        _signal.signal(_signal.SIGINT, _shutdown)

        logger.info("telegram_bot_starting chat_id=%s", self._chat_id)
        _send(
            self._token,
            self._chat_id,
            header("🟢", "Boomer bot online", stamp()) + "\n" + note("Send /status."),
        )
        while True:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    try:
                        self._dispatch(update)
                    except Exception as exc:
                        logger.error(
                            "dispatch_error update_id=%s: %s", update.get("update_id"), exc
                        )
            except SystemExit:
                raise
            except Exception as exc:
                logger.error("polling_error: %s", exc)
                time.sleep(5)


def from_env(db_path: str | None = None) -> TelegramBot:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    path = db_path or os.environ.get("BOOMER_DB_PATH", "/var/lib/boomer/boomer.db")
    return TelegramBot(token=token, chat_id=chat_id, db_path=path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from_env().run_forever()
