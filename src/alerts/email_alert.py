"""Email alert sender — CRITICAL severity only.

Uses stdlib smtplib with STARTTLS. Configured via environment variables or
the secrets.env file at startup. Intended as the mandatory fallback channel
for critical alerts when Telegram is unavailable.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
) -> bool:
    """Send a plain-text email via SMTP with STARTTLS. Return True on success."""
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(username, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info("email_sent to=%s subject=%s", to_addr, subject)
        return True
    except Exception as exc:
        logger.error("email_send_failed error=%s", exc)
        return False


def format_email_subject(severity: str, title: str) -> str:
    return f"[BOOMER {severity}] {title}"
