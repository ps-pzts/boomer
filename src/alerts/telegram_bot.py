"""Telegram command bot for Boomer operator interface.

Supports two commands:
  /status  — system snapshot: mode, signals, pending approvals, open positions, today's PnL
  /screen  — list today's awaiting_human recommendations with ✅/❌ inline buttons

Inline button callbacks:
  approve:<rec_id>  — approve a recommendation
  reject:<rec_id>   — reject with default reason

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


def _call(token: str, method: str, payload: dict) -> dict:
    url = _API.format(token=token, method=method)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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


def _answer_callback(token: str, callback_id: str, text: str = "") -> None:
    _call(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def _edit_message_text(token: str, chat_id: str | int, message_id: int, text: str) -> None:
    _call(
        token,
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        },
    )


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
        approvals = conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE status='awaiting_human'"
        ).fetchone()[0]
        lt_open = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE is_open=1 AND track='long_term'"
        ).fetchone()[0]
        sw_open = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE is_open=1 AND track='swing'"
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
    finally:
        conn.close()
    return {
        "mode": mode,
        "run_date": run_date,
        "signals": signals,
        "approvals": approvals,
        "lt_open": lt_open,
        "sw_open": sw_open,
        "pnl": float(pnl),
        "circuit_breakers": circuit_breakers,
    }


def _get_pending_recs(db_path: str) -> list[dict]:
    conn = _db_conn(db_path)
    try:
        rows = conn.execute(
            """SELECT r.recommendation_id, r.stock_symbol, r.exchange, r.track,
                      r.entry_zone_low, r.entry_zone_high, r.stop_loss_price, r.target_price,
                      r.position_size_shares,
                      COALESCE(tp.reward_to_risk, 0) as rr,
                      COALESCE(tp.expected_value_per_share, 0) as ev,
                      s.confidence,
                      COALESCE(sc.sector, 'Unknown') as sector,
                      COALESCE(
                          (SELECT close FROM prices
                           WHERE stock_symbol=r.stock_symbol AND exchange=r.exchange
                           ORDER BY trade_date DESC LIMIT 1), 0
                      ) as cmp
               FROM recommendations r
               LEFT JOIN trade_plans tp ON tp.plan_id = r.plan_id
               JOIN signals s ON s.signal_id = r.signal_id
               LEFT JOIN sector_classifications sc ON sc.symbol = r.stock_symbol
               WHERE r.status = 'awaiting_human'
               ORDER BY s.confidence DESC""",
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _approve_rec(db_path: str, rec_id: str) -> bool:
    conn = _db_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE recommendations SET status='approved_by_apm'"
            " WHERE recommendation_id=? AND status='awaiting_human'",
            (rec_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _reject_rec(db_path: str, rec_id: str) -> bool:
    conn = _db_conn(db_path)
    try:
        sql = (
            "UPDATE recommendations SET status='rejected_by_apm',"
            " decision_reason='operator_rejected'"
            " WHERE recommendation_id=? AND status='awaiting_human'"
        )
        cur = conn.execute(
            sql,
            (rec_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Message formatters ─────────────────────────────────────────────────────────


def _fmt_status(snap: dict) -> str:
    mode_emoji = {"auto": "🟢", "paused": "🟡", "emergency_stop": "🔴"}.get(snap["mode"], "⚪")
    cb = ""
    if snap["circuit_breakers"]:
        cb = "\n⚡ <b>Circuit breakers:</b> " + ", ".join(snap["circuit_breakers"])
    pnl_sign = "+" if snap["pnl"] >= 0 else ""
    return (
        f"<b>📊 Boomer Status — {snap['run_date']}</b>\n\n"
        f"{mode_emoji} Mode: <b>{snap['mode']}</b>\n"
        f"📡 Signals today: {snap['signals']}\n"
        f"⏳ Pending approvals: <b>{snap['approvals']}</b>\n\n"
        f"<b>Open positions</b>\n"
        f"  Long-term: {snap['lt_open']}\n"
        f"  Swing: {snap['sw_open']}\n\n"
        f"💰 Today's P&L: {pnl_sign}₹{snap['pnl']:,.0f}"
        f"{cb}"
    )


def _fmt_rec_card(idx: int, r: dict) -> str:
    track_short = {"long_term": "LT", "swing": "SW", "intraday": "ID"}.get(r["track"], r["track"])
    short_id = r["recommendation_id"][:8]
    return (
        f"<b>#{idx} {r['stock_symbol']}</b> [{track_short}] — {r['sector']}\n"
        f"  CMP ₹{r['cmp']:.0f} | Entry ₹{r['entry_zone_low']:.0f}–{r['entry_zone_high']:.0f}\n"
        f"  SL ₹{r['stop_loss_price']:.0f} | Target ₹{r['target_price']:.0f}\n"
        f"  RR {r['rr']:.1f}x | EV ₹{r['ev']:.1f} | Conf {r['confidence']:.0%}\n"
        f"  Qty {r['position_size_shares']} shares | ID: <code>{short_id}</code>"
    )


def _rec_inline_keyboard(rec_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve:{rec_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{rec_id}"},
            ]
        ]
    }


# ── Command handlers ───────────────────────────────────────────────────────────


def handle_status(token: str, chat_id: str | int, db_path: str) -> None:
    snap = _get_snapshot(db_path)
    _send(token, chat_id, _fmt_status(snap))


def handle_screen(token: str, chat_id: str | int, db_path: str) -> None:
    recs = _get_pending_recs(db_path)
    if not recs:
        _send(token, chat_id, "✅ No recommendations awaiting approval.")
        return
    _send(token, chat_id, f"<b>⏳ {len(recs)} recommendation(s) awaiting approval</b>")
    for idx, rec in enumerate(recs, 1):
        _send(
            token,
            chat_id,
            _fmt_rec_card(idx, rec),
            reply_markup=_rec_inline_keyboard(rec["recommendation_id"]),
        )


def handle_callback(token: str, callback: dict, db_path: str) -> None:
    callback_id = callback["id"]
    data = callback.get("data", "")
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    original_text = callback["message"].get("text", "")

    if data.startswith("approve:"):
        rec_id = data[len("approve:") :]
        ok = _approve_rec(db_path, rec_id)
        if ok:
            _answer_callback(token, callback_id, "✅ Approved")
            _edit_message_text(token, chat_id, message_id, original_text + "\n\n<b>✅ APPROVED</b>")
        else:
            _answer_callback(token, callback_id, "Already actioned or not found")

    elif data.startswith("reject:"):
        rec_id = data[len("reject:") :]
        ok = _reject_rec(db_path, rec_id)
        if ok:
            _answer_callback(token, callback_id, "❌ Rejected")
            _edit_message_text(token, chat_id, message_id, original_text + "\n\n<b>❌ REJECTED</b>")
        else:
            _answer_callback(token, callback_id, "Already actioned or not found")

    else:
        _answer_callback(token, callback_id)


# ── Polling loop ───────────────────────────────────────────────────────────────


class TelegramBot:
    def __init__(self, token: str, chat_id: str, db_path: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db_path = db_path
        self._offset = 0

    def _get_updates(self) -> list[dict]:
        result = _call(
            self._token,
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        if not result.get("ok"):
            return []
        return result.get("result", [])

    def _dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            handle_callback(self._token, update["callback_query"], self._db_path)
            return

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
        elif text in ("/screen", "/screen@boomerbot", "/approvals", "/approvals@boomerbot"):
            handle_screen(self._token, msg["chat"]["id"], self._db_path)
        elif text in ("/help", "/start"):
            _send(
                self._token,
                msg["chat"]["id"],
                "<b>Boomer Bot Commands</b>\n\n"
                "/status — system snapshot (mode, signals, P&amp;L)\n"
                "/screen — pending recommendations with approve/reject buttons\n",
            )

    def run_forever(self) -> None:
        logger.info("telegram_bot_starting chat_id=%s", self._chat_id)
        _send(self._token, self._chat_id, "🤖 <b>Boomer bot online.</b> Send /status or /screen.")
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
