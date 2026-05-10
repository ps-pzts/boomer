"""Telegram alert sender.

Uses the Telegram Bot API (sendMessage) with a simple HTTP POST.
No telegram SDK dependency — stdlib urllib only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to a Telegram chat. Return True on success."""
    url = _TELEGRAM_API.format(token=token)
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": parse_mode}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                return True
            logger.error("telegram_api_error response=%s", body)
            return False
    except urllib.error.HTTPError as exc:
        logger.error("telegram_http_error status=%d reason=%s", exc.code, exc.reason)
        return False
    except Exception as exc:
        logger.error("telegram_send_failed error=%s", exc)
        return False


def format_alert_text(severity: str, title: str, body: str) -> str:
    """Format an alert as HTML for Telegram."""
    emoji = {"INFO": "ℹ️", "WARN": "⚠️", "CRITICAL": "🚨"}.get(severity, "📢")
    return f"{emoji} <b>[{severity}] {title}</b>\n\n{body}"
