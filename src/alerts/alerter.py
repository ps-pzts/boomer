"""AlertManager — batching, dual-channel routing, and missed-alert persistence.

Routing rules:
- INFO     → daily summary only (caller batches; not per-event)
- WARN     → Telegram only; batched (one send per 6h window)
- CRITICAL → Telegram + email simultaneously; always immediate

If both channels fail for a CRITICAL alert, the alert is written to
`critical_notification_failures` so the dashboard can surface it on next load.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
import threading

from .email_alert import format_email_subject, send_email
from .models import Alert, AlertSeverity
from .telegram import format_alert_text, send_telegram

logger = logging.getLogger(__name__)

# Singleton alerter — initialised once at process start
_alerter: AlertManager | None = None
_alerter_lock = threading.Lock()


def get_alerter() -> AlertManager:
    global _alerter
    with _alerter_lock:
        if _alerter is None:
            _alerter = AlertManager.from_env()
    return _alerter


class AlertManager:
    """Send alerts on the appropriate channel(s) with batching and persistence."""

    def __init__(
        self,
        db_path: str,
        telegram_token: str | None,
        telegram_chat_id: str | None,
        smtp_host: str | None,
        smtp_port: int,
        smtp_user: str | None,
        smtp_password: str | None,
        email_from: str | None,
        email_to: str | None,
        warn_batch_hours: int = 6,
    ) -> None:
        self._db_path = db_path
        self._tg_token = telegram_token
        self._tg_chat = telegram_chat_id
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_pass = smtp_password
        self._email_from = email_from
        self._email_to = email_to
        self._warn_batch_hours = warn_batch_hours
        self._warn_buffer: list[Alert] = []
        self._last_warn_flush: datetime.datetime = datetime.datetime(2000, 1, 1)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, db_path: str | None = None) -> AlertManager:
        return cls(
            db_path=db_path or os.environ.get("BOOMER_DB_PATH", "/var/lib/boomer/boomer.db"),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            smtp_host=os.environ.get("ALERT_SMTP_HOST"),
            smtp_port=int(os.environ.get("ALERT_SMTP_PORT", "587")),
            smtp_user=os.environ.get("ALERT_SMTP_USER"),
            smtp_password=os.environ.get("ALERT_SMTP_PASSWORD"),
            email_from=os.environ.get("ALERT_EMAIL_FROM"),
            email_to=os.environ.get("ALERT_EMAIL_TO"),
        )

    # ─── Public API ───────────────────────────────────────────────────────────

    def info(self, title: str, body: str, source_task_id: str | None = None) -> None:
        """Queue an INFO alert. INFO is only sent as a daily summary — not per-event."""
        logger.info("alert_info title=%s", title)
        self._persist(Alert(AlertSeverity.INFO, title, body, source_task_id, [], []))

    def warn(self, title: str, body: str, source_task_id: str | None = None) -> None:
        """Buffer WARN alert; flush if 6h batch window elapsed."""
        alert = Alert(AlertSeverity.WARN, title, body, source_task_id)
        with self._lock:
            self._warn_buffer.append(alert)
            now = datetime.datetime.utcnow()
            hours_since = (now - self._last_warn_flush).total_seconds() / 3600
            if hours_since >= self._warn_batch_hours:
                self._flush_warn_buffer(now)

    def critical(self, title: str, body: str, source_task_id: str | None = None) -> None:
        """Send CRITICAL immediately on both channels."""
        alert = Alert(AlertSeverity.CRITICAL, title, body, source_task_id)
        tg_ok = self._send_telegram(alert)
        email_ok = self._send_email(alert)
        alert.channels_ok = [c for c, ok in [("telegram", tg_ok), ("email", email_ok)] if ok]
        self._persist(alert)
        if not tg_ok and not email_ok:
            self._record_missed_critical(alert)

    def send_daily_summary(self, title: str, body: str) -> None:
        """Send the daily INFO summary immediately (called by eod_reconciliation task)."""
        alert = Alert(AlertSeverity.INFO, title, body, channels_tried=["telegram"], channels_ok=[])
        ok = self._send_telegram(alert)
        alert.channels_ok = ["telegram"] if ok else []
        self._persist(alert)

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _flush_warn_buffer(self, now: datetime.datetime) -> None:
        if not self._warn_buffer:
            return
        combined_body = "\n\n".join(f"• {a.title}: {a.body}" for a in self._warn_buffer)
        batch_alert = Alert(
            severity=AlertSeverity.WARN,
            title=f"WARN summary ({len(self._warn_buffer)} items)",
            body=combined_body,
        )
        batch_alert.channels_tried = ["telegram"]
        ok = self._send_telegram(batch_alert)
        batch_alert.channels_ok = ["telegram"] if ok else []
        self._persist(batch_alert)
        logger.info("warn_flush count=%d ok=%s", len(self._warn_buffer), ok)
        self._warn_buffer.clear()
        self._last_warn_flush = now

    def _send_telegram(self, alert: Alert) -> bool:
        if not self._tg_token or not self._tg_chat:
            logger.warning("telegram_not_configured alert=%s", alert.title)
            alert.channels_tried.append("telegram")
            return False
        alert.channels_tried.append("telegram")
        text = format_alert_text(alert.severity, alert.title, alert.body)
        return send_telegram(self._tg_token, self._tg_chat, text)

    def _send_email(self, alert: Alert) -> bool:
        required = [
            self._smtp_host, self._smtp_user, self._smtp_pass, self._email_from, self._email_to
        ]
        if not all(required):
            logger.warning("email_not_configured alert=%s", alert.title)
            alert.channels_tried.append("email")
            return False
        alert.channels_tried.append("email")
        return send_email(
            smtp_host=self._smtp_host,  # type: ignore[arg-type]
            smtp_port=self._smtp_port,
            username=self._smtp_user,  # type: ignore[arg-type]
            password=self._smtp_pass,  # type: ignore[arg-type]
            from_addr=self._email_from,  # type: ignore[arg-type]
            to_addr=self._email_to,  # type: ignore[arg-type]
            subject=format_email_subject(alert.severity, alert.title),
            body=alert.body,
        )

    def _persist(self, alert: Alert) -> None:
        now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """INSERT INTO alert_log
                   (severity, title, body, sent_at, channels_tried, channels_ok, source_task_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    alert.severity, alert.title, alert.body, now,
                    json.dumps(alert.channels_tried), json.dumps(alert.channels_ok),
                    alert.source_task_id,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error("alert_persist_failed error=%s", exc)

    def _record_missed_critical(self, alert: Alert) -> None:
        now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """INSERT INTO critical_notification_failures (title, body, failed_at)
                   VALUES (?,?,?)""",
                (alert.title, alert.body, now),
            )
            conn.commit()
            conn.close()
            logger.critical("missed_critical_alert title=%s both_channels_failed=True", alert.title)
        except Exception as exc:
            logger.error("missed_critical_persist_failed error=%s", exc)
