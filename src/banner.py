"""Avengers-themed startup logging for Boomer.

Codename mapping:
  FURY        — Orchestrator       (Nick Fury: director, runs the whole operation)
  SHIELD      — Capital & Risk     (S.H.I.E.L.D.: protects the assets)
  HAWKEYE     — Collector          (Clint Barton: eyes on every data source)
  JARVIS      — Brain              (Tony's AI: intelligence and signal engine)
  WAR MACHINE — Executor           (James Rhodes: heavy iron, places real orders)
  BLACK WIDOW — Alerts             (Natasha: silent operative, sends notifications)
  FRIDAY      — Dashboard          (Tony's visual interface)
"""

from __future__ import annotations

import logging
import sys

_BANNER = """
  ╔══════════════════════════════════════════════════════╗
  ║   B O O M E R  ─  Autonomous Trading Intelligence   ║
  ║             Disciplined · Data-driven · Live         ║
  ╚══════════════════════════════════════════════════════╝
"""

_BOOT_AGENTS: list[tuple[str, str]] = [
    ("FURY", "Orchestrator — scheduling engine armed"),
    ("SHIELD", "Capital & risk — circuit breakers armed"),
    ("HAWKEYE", "Collector grid — watching NSE + BSE data sources"),
    ("JARVIS", "Brain framework — signal & recommendation engine ready"),
    ("WAR MACHINE", "Executor — Kite (intraday) + Fyers (delivery) standing by"),
    ("BLACK WIDOW", "Alert system — Telegram + email channels active"),
]

_log = logging.getLogger("boomer.boot")


def print_banner() -> None:
    """Write ASCII banner to stdout before structured logging begins."""
    sys.stdout.write(_BANNER)
    sys.stdout.flush()


def log_boot_sequence(db_path: str, poll_interval: int) -> None:
    """Log all subsystem online messages at orchestrator startup."""
    for name, desc in _BOOT_AGENTS:
        _log.info("[%-11s] %s", name, desc)
    _log.info("[FURY] All agents assembled — db=%s poll=%ds", db_path, poll_interval)
    _notify_telegram(
        "🟢 <b>Boomer online</b>\n"
        "All agents assembled — orchestrator armed and polling.\n"
        "Send /status for system snapshot."
    )


def log_crash_recovery(interrupted_count: int) -> None:
    """Log crash recovery outcome (called after marking stale RUNNING tasks)."""
    if interrupted_count:
        _log.warning(
            "[FURY] Crash recovery — %d stale task(s) marked INTERRUPTED",
            interrupted_count,
        )
        _notify_telegram(
            f"⚠️ <b>Boomer restarted after crash</b>\n"
            f"{interrupted_count} stale task(s) marked INTERRUPTED from previous run.\n"
            "Likely an ungraceful shutdown — check logs."
        )
    else:
        _log.info("[FURY] Clean start — no interrupted tasks from previous run")


def log_shutdown(signum: int) -> None:
    _log.info("[FURY] Standing down — signal=%d received, shutting down gracefully", signum)
    _notify_telegram(
        f"🔴 <b>Boomer going offline</b>\n"
        f"Graceful shutdown — signal {signum} received.\n"
        "Orchestrator stopped. No tasks will run until restarted."
    )


def _notify_telegram(message: str) -> None:
    """Best-effort Telegram push — never raises, never blocks startup."""
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        from alerts.telegram import send_telegram
        send_telegram(token, chat_id, message)
    except Exception as exc:
        _log.warning("startup_telegram_notify_failed: %s", exc)


def log_dashboard_online() -> None:
    sys.stdout.write("  [FRIDAY] Dashboard live — WebSocket feed active\n")
    sys.stdout.flush()
