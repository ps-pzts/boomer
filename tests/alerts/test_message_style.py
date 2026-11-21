"""#139: every outgoing Telegram message site speaks the shared visual language.

`is_card` fails when a message regresses to plain text: a bold icon header, balanced tags,
under Telegram's limit. Numbers are covered by each site's own tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from alerts import commands, telegram_bot
from alerts.telegram import format_alert_text
from executor import kite_checks
from tests.alerts.plain import balanced, is_card
from tests.alerts.test_commands import bracket, db, position, session, sql  # noqa: F401

READINESS = sys.modules["alerts.commands.readiness"]  # the package re-binds the name


@pytest.mark.parametrize(
    "cmd",
    ["/status", "/positions", "/trades", "/pnl", "/halt", "/readiness", "/help"],
)
def test_empty_day_replies_are_cards(db, cmd):  # noqa: F811
    text = commands.HELP if cmd == "/help" else commands.run(db, cmd)
    if cmd == "/halt":  # no session today: a one-line status, not a card
        assert text.startswith("⚪ ")
    else:
        assert is_card(text)


def test_busy_day_replies_are_cards(db):  # noqa: F811
    session(db, "halted", "reconcile_mismatch", {"failed_checks": ["c1"], "c1": {"ok": False}})
    bracket(db, "M&M", "closed", pnl=40.0, reason="target_hit")
    position(db, "INFY", unprotected=1)
    for cmd in ("/status", "/positions", "/trades", "/pnl", "/halt"):
        text = commands.run(db, cmd)
        assert is_card(text), cmd
    assert "M&amp;M" in commands.run(db, "/trades")  # symbols are escaped
    assert "<blockquote>" in commands.run(db, "/status")  # halt hint
    assert "🚨 <b>UNPROTECTED</b>" in commands.run(db, "/positions")


def test_control_replies_are_notes_with_their_instruction(db):  # noqa: F811
    assert "<blockquote>" in commands.run(db, "/emergency")
    assert "<code>/emergency confirm</code>" in commands.run(db, "/emergency")
    assert commands.run(db, "/pause").startswith("🟡 <b>Paused</b>\n<blockquote>")
    assert commands.run(db, "/resume").startswith("🟢 <b>Resumed</b>")
    assert commands.run(db, "/emergency confirm").startswith("🔴 <b>EMERGENCY STOP engaged</b>")
    assert commands.run(db, "/pause").startswith("⛔ <b>Emergency stop is active</b>")
    assert commands.run(db, "/ack").startswith("⛔ <b>Nothing to acknowledge</b>")
    session(db, "halted", "reconcile_mismatch")
    assert commands.run(db, "/ack").startswith("✅ <b>Acknowledged</b>\n<blockquote>")


def test_readiness_marks_each_criterion_with_a_dot(monkeypatch):
    from executor.live_readiness import Criterion, Report, Status

    crits = [
        Criterion(name=n, status=s, summary=f"{n} <1>")
        for n, s in (("a", Status.PASS), ("b", Status.FAIL), ("c", Status.INSUFFICIENT_DATA))
    ]
    monkeypatch.setattr(READINESS, "build_report", lambda d: Report(crits))
    text = commands.run("db", "/readiness")
    assert is_card(text)
    assert "🟢 <b>a</b>: a &lt;1&gt;" in text and "🔴 <b>b</b>" in text and "🟡 <b>c</b>" in text
    assert "🔴 <b>NOT ready</b>" in text and "<blockquote>Evidence only" in text


def test_readiness_error_is_escaped(monkeypatch):
    monkeypatch.setattr(READINESS, "build_report", MagicMock(side_effect=OSError("<no db>")))
    assert commands.run("db", "/readiness") == "⛔ Readiness report unavailable: &lt;no db&gt;"


def test_mode_replies(db, monkeypatch):  # noqa: F811
    text = telegram_bot.handle_mode(db, "/mode")
    assert is_card(text) and "<pre>" in text
    monkeypatch.setenv("BOOMER_TRADING_MODE", "live")
    monkeypatch.setattr(
        "executor.trading_mode.build_report", lambda d: SimpleNamespace(criteria=[], ready=True)
    )
    ask = telegram_bot.handle_mode(db, "/mode live")
    assert ask.startswith("⛔") and "<blockquote>Send <code>/mode live confirm</code>" in ask


def test_canary_reply_is_a_card(monkeypatch):
    rep = SimpleNamespace(ok=False, kind="preflight", failures=lambda: ["a<b"])
    monkeypatch.setattr(kite_checks, "kite_broker_from_env", lambda: object())
    monkeypatch.setattr(kite_checks, "rerun", lambda d, b: [rep])
    text = telegram_bot.handle_canary("db")
    assert is_card(text) and "a&lt;b" in text and "<blockquote>canary not run" in text


def test_bot_online_and_offline_messages(monkeypatch):
    sent: list[str] = []
    handlers: dict = {}
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    monkeypatch.setattr("signal.signal", lambda sig, h: handlers.setdefault(sig, h))

    def send(token, chat, text, *a):
        sent.append(text)
        if len(sent) == 1:
            raise SystemExit  # stop run_forever right after the online message

    monkeypatch.setattr(telegram_bot, "_send", send)
    with pytest.raises(SystemExit):
        telegram_bot.TelegramBot("t", "1", "db").run_forever()
    with pytest.raises(SystemExit):
        next(iter(handlers.values()))(15, None)
    assert sent[0].startswith("<b>🟢 Boomer bot online</b>") and "/status" in sent[0]
    assert sent[1].startswith("<b>🔴 Boomer bot going offline</b>") and "signal 15" in sent[1]
    assert all(is_card(m) for m in sent)


def test_command_failure_is_escaped(db, monkeypatch):  # noqa: F811
    sent: list[str] = []
    monkeypatch.setattr(telegram_bot, "_send", lambda t, c, text, *a: sent.append(text))
    monkeypatch.setitem(commands.HANDLERS, "/pnl", MagicMock(side_effect=ValueError("<x>")))
    telegram_bot.TelegramBot("t", "1", db)._dispatch(
        {"message": {"chat": {"id": 1}, "text": "/pnl"}}
    )
    assert sent == ["⛔ <b>/pnl failed</b>: &lt;x&gt;"]


@pytest.mark.parametrize("severity, icon", [("INFO", "🟢"), ("WARN", "🟡"), ("CRITICAL", "🔴")])
def test_alerts_use_severity_dots_and_escape_the_body(severity, icon):
    text = format_alert_text(severity, "Feed <stalled>", "M&M <b>")
    assert text.startswith(f"<b>{icon} {severity} · Feed &lt;stalled&gt;</b>\n<i>")
    assert "<blockquote>M&amp;M &lt;b&gt;</blockquote>" in text and balanced(text)
