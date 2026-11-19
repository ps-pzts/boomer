"""#85: Telegram /canary re-runs today's Kite checks."""

from __future__ import annotations

from src.alerts import telegram_bot
from src.executor import kite_checks
from src.executor.preflight import CheckReport, CheckResult


def _rep(kind: str, ok: bool) -> CheckReport:
    return CheckReport(kind, (CheckResult("token", ok, "AB1234" if ok else "expired"),), "t")


def test_both_pass(monkeypatch):
    monkeypatch.setattr(kite_checks, "kite_broker_from_env", lambda: object())
    monkeypatch.setattr(
        kite_checks, "rerun", lambda db, broker: [_rep("preflight", True), _rep("canary", True)]
    )
    text = telegram_bot.handle_canary("db")
    assert "✅ preflight passed" in text and "✅ canary passed" in text
    assert "resume" in text


def test_failure_lists_the_failed_checks(monkeypatch):
    monkeypatch.setattr(kite_checks, "kite_broker_from_env", lambda: object())
    monkeypatch.setattr(kite_checks, "rerun", lambda db, broker: [_rep("preflight", False)])
    text = telegram_bot.handle_canary("db")
    assert "⛔ preflight FAILED" in text and "token: expired" in text
    assert "canary not run" in text


def test_no_credentials(monkeypatch):
    monkeypatch.setattr(kite_checks, "kite_broker_from_env", lambda: None)
    called = []
    monkeypatch.setattr(kite_checks, "rerun", lambda db, broker: called.append(broker) or [])
    assert "KITE_API_KEY" in telegram_bot.handle_canary("db")
    assert called == []


def test_broker_build_error_is_reported(monkeypatch):
    def boom():
        raise RuntimeError("kiteconnect missing")

    monkeypatch.setattr(kite_checks, "kite_broker_from_env", boom)
    assert "kiteconnect missing" in telegram_bot.handle_canary("db")


def test_dispatch_routes_canary(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_bot, "_send", lambda token, chat, text, *a: sent.append(text))
    monkeypatch.setattr(telegram_bot, "handle_canary", lambda db: "CANARY-REPLY")
    bot = telegram_bot.TelegramBot("tok", "42", "db")
    bot._dispatch({"message": {"chat": {"id": 42}, "text": "/canary"}})
    assert sent[-1] == "CANARY-REPLY"
    bot._dispatch({"message": {"chat": {"id": 42}, "text": "/help"}})
    assert "/canary" in sent[-1]
