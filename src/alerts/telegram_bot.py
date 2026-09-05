"""Telegram command bot for Boomer operator interface.

Supported commands:
  /status  — snapshot: mode, broker session health, signals, queued GTTs, P&L

Uses Telegram Bot API (long-polling getUpdates). No SDK; stdlib urllib only.
Run as a standalone process alongside the orchestrator.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

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
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _call(token, "sendMessage", payload)


# ── Database helpers ───────────────────────────────────────────────────────────


def _db_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def _get_snapshot(db_path: str) -> dict:
    conn = _db_conn(db_path)
    run_date = _today_ist()
    try:
        mode_row = conn.execute("SELECT mode FROM bot_mode WHERE id=1").fetchone()
        mode = mode_row["mode"] if mode_row else "auto"
        signals = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE DATE(generated_at)=?", (run_date,)
        ).fetchone()[0]
        queued = conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE status='queued_for_execution'"
        ).fetchone()[0]
        submitted_today = conn.execute(
            "SELECT COUNT(*) FROM recommendations"
            " WHERE status='submitted_to_broker' AND DATE(submitted_at)=?",
            (run_date,),
        ).fetchone()[0]
        intraday_open = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE is_open=1 AND track='intraday'"
        ).fetchone()[0]
        pnl = conn.execute(
            "SELECT COALESCE(SUM(realised_pnl),0) FROM positions WHERE DATE(entry_at)=?",
            (run_date,),
        ).fetchone()[0]
        circuit_breakers = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT breaker_name FROM circuit_breaker_events"
                " WHERE event_type='tripped' AND DATE(event_time)=?",
                (run_date,),
            ).fetchall()
        ]
        # Broker session: check if pre_market_executor_setup ran successfully today
        broker_row = conn.execute(
            "SELECT started_at FROM task_runs"
            " WHERE task_id='pre_market_executor_setup' AND status='SUCCESS'"
            " AND DATE(started_at)=? ORDER BY started_at DESC LIMIT 1",
            (run_date,),
        ).fetchone()
        broker_refreshed_at = broker_row["started_at"][:5] if broker_row else None
    finally:
        conn.close()
    return {
        "mode": mode,
        "run_date": run_date,
        "signals": signals,
        "queued": queued,
        "submitted_today": submitted_today,
        "intraday_open": intraday_open,
        "pnl": float(pnl),
        "circuit_breakers": circuit_breakers,
        "broker_refreshed_at": broker_refreshed_at,
    }


# ── Message formatters ─────────────────────────────────────────────────────────


def _fmt_status(snap: dict) -> str:
    mode_emoji = {"auto": "🟢", "paused": "🟡", "emergency_stop": "🔴"}.get(snap["mode"], "⚪")
    cb = ""
    if snap["circuit_breakers"]:
        cb = "\n⚡ <b>Circuit breakers:</b> " + ", ".join(snap["circuit_breakers"])
    pnl_sign = "+" if snap["pnl"] >= 0 else ""
    if snap["broker_refreshed_at"]:
        broker_line = f"🔐 Broker session: ✅ refreshed at {snap['broker_refreshed_at']} IST"
    else:
        broker_line = "🔐 Broker session: ⚠️ not refreshed today"
    generated_at = datetime.now(IST).strftime("%d %b %Y %H:%M:%S IST")
    return (
        f"<b>📊 Boomer Status — {snap['run_date']}</b>\n\n"
        f"{mode_emoji} Mode: <b>{snap['mode']}</b>\n"
        f"{broker_line}\n\n"
        f"<b>Recommendations</b>\n"
        f"  📡 Signals today: {snap['signals']}\n"
        f"  🚀 Queued for GTT: {snap['queued']}\n"
        f"  ✅ Submitted today: {snap['submitted_today']}\n\n"
        f"<b>Open positions</b>\n"
        f"  Intraday: {snap['intraday_open']}\n\n"
        f"💰 Today's P&amp;L: {pnl_sign}₹{snap['pnl']:,.0f}"
        f"{cb}\n\n"
        f"<i>Generated at {generated_at}</i>"
    )


# ── Command handlers ───────────────────────────────────────────────────────────


def handle_status(token: str, chat_id: str | int, db_path: str) -> None:
    snap = _get_snapshot(db_path)
    _send(token, chat_id, _fmt_status(snap))


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
        if text in ("/status", "/status@boomerbot"):
            handle_status(self._token, msg["chat"]["id"], self._db_path)
        elif text in ("/help", "/start"):
            _send(
                self._token,
                msg["chat"]["id"],
                "<b>Boomer Bot Commands</b>\n\n"
                "/status — system snapshot: mode, broker session, signals, queued GTTs, P&amp;L\n",
            )

    def run_forever(self) -> None:
        import signal as _signal

        def _shutdown(signum: int, frame: object) -> None:
            logger.info("telegram_bot_stopping signal=%d", signum)
            _send(
                self._token, self._chat_id,
                f"🔴 <b>Boomer bot going offline</b>\n"
                f"Graceful shutdown (signal {signum}). "
                "Commands will not be processed until restarted."
            )
            raise SystemExit(0)

        _signal.signal(_signal.SIGTERM, _shutdown)
        _signal.signal(_signal.SIGINT, _shutdown)

        logger.info("telegram_bot_starting chat_id=%s", self._chat_id)
        _send(self._token, self._chat_id, "🤖 <b>Boomer bot online.</b> Send /status.")
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
