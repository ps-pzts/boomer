"""#139: the orchestrator-side Telegram messages (boot, check-ins, login) use the shared card
format; see tests/alerts/test_message_style.py for the bot and alert sites."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.alerts.plain import is_card, plain
from tests.alerts.test_commands import db, sql  # noqa: F401


def sent_text(send: MagicMock) -> str:
    return send.call_args.args[-1]


def test_orchestrator_banner_messages():
    import banner

    with patch.object(banner, "_notify_telegram") as notify:
        banner.log_boot_sequence("db", 5)
        banner.log_crash_recovery(2)
        banner.log_shutdown(15)
    online, crash, offline = (c.args[0] for c in notify.call_args_list)
    assert online.startswith("<b>🟢 Boomer online</b>")
    assert crash.startswith("<b>🟡 Boomer restarted after crash</b>") and "2 stale" in crash
    assert offline.startswith("<b>🔴 Boomer going offline</b>") and "signal 15" in offline
    assert all(is_card(m) and "<blockquote>" in m for m in (online, crash, offline))


def test_morning_check_in(db):  # noqa: F811
    from orchestrator.tasks_maintenance import _morning_heartbeat

    sql(
        db,
        "INSERT INTO circuit_breaker_events (event_id, breaker_name, event_type, event_time,"
        " created_at) VALUES ('e1', 'nifty_move', 'tripped', '2026-11-21T09:00:00', 't')",
    )
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    with patch.dict(os.environ, env), patch("alerts.telegram.send_telegram") as send:
        _morning_heartbeat("2026-11-21", 1, db)
    text = sent_text(send)
    assert is_card(text) and plain(text).startswith("🟢 Morning check-in")
    assert "Date 2026-11-21 Queued for execution 0" in plain(text)
    assert "🟡 <b>Circuit breakers:</b> nifty_move" in text


def test_nightly_health_check_issues_are_a_card(db):  # noqa: F811
    from orchestrator.tasks_maintenance import _nightly_health_check

    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    with (
        patch.dict(os.environ, env),
        patch("alerts.telegram.send_telegram") as send,
        patch("shutil.disk_usage", return_value=SimpleNamespace(free=5, total=100)),
    ):
        _nightly_health_check("2026-11-21", 1, db)
    text = sent_text(send)
    assert is_card(text) and "issues found" in text
    assert "🟡 Low disk space" in text and "<blockquote>Review before market open." in text


@pytest.mark.parametrize("error, icon", [(False, "🟢"), (True, "🔴")])
def test_auto_login_notifications(error, icon):
    from executor import auto_login

    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    with patch.dict(os.environ, env), patch("alerts.telegram.send_telegram") as send:
        auto_login._notify("Kite token refreshed", "<pre>x</pre>", error=error)
    text = sent_text(send)
    assert text.startswith(f"<b>{icon} Kite token refreshed</b>\n<i>") and is_card(text)
