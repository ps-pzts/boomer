"""#85: operator re-run of the Kite checks (Telegram /canary)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import kiteconnect
import pytest

from executor import kite_checks as kc
from tests.executor.kite_fake import FakeKite
from tests.executor.test_kite_checks import _MIGRATIONS

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture()
def db_path(tmp_path) -> str:
    from db.migrations import run_migrations

    p = str(tmp_path / "r.db")
    run_migrations(p, _MIGRATIONS)
    return p


def _rerun(db_path, broker, at: datetime, tmp_path):
    return kc.rerun(
        db_path,
        broker,
        canary_config=kc.CanaryConfig(recordings_dir=str(tmp_path / "rec")),
        clock=lambda: at,
        sleep=lambda s: None,
    )


def _gate(db_path: str, day: date) -> str | None:
    c = sqlite3.connect(db_path)
    try:
        return kc.gate(c, day)
    finally:
        c.close()


def test_market_hours_runs_both_and_clears_the_gate(db_path, tmp_path):
    at = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    reports = _rerun(db_path, FakeKite().broker(), at, tmp_path)
    assert [(r.kind, r.ok) for r in reports] == [("preflight", True), ("canary", True)]
    assert _gate(db_path, at.date()) is None


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 11, 19, 9, 5, tzinfo=IST),  # pre-open: stop orders rejected
        datetime(2026, 11, 19, 15, 25, tzinfo=IST),  # after 15:20 MIS cutoff
        datetime(2026, 11, 21, 10, 0, tzinfo=IST),  # Saturday
    ],
)
def test_outside_market_hours_only_preflight_runs(db_path, tmp_path, at):
    fk = FakeKite()
    reports = _rerun(db_path, fk.broker(), at, tmp_path)
    assert [r.kind for r in reports] == ["preflight"]
    fk.sdk.place_order.assert_not_called()


def test_failed_preflight_skips_the_canary(db_path, tmp_path):
    fk = FakeKite()
    fk.sdk.profile.side_effect = kiteconnect.exceptions.TokenException("expired")
    at = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    reports = _rerun(db_path, fk.broker(), at, tmp_path)
    assert [(r.kind, r.ok) for r in reports] == [("preflight", False)]
    fk.sdk.place_order.assert_not_called()
    assert _gate(db_path, at.date()) == "preflight_failed"


def test_rerun_rows_are_marked_operator(db_path, tmp_path):
    at = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    _rerun(db_path, FakeKite().broker(), at, tmp_path)
    c = sqlite3.connect(db_path)
    assert {r[0] for r in c.execute("SELECT source FROM kite_daily_checks")} == {"operator"}


def test_kite_broker_from_env_file_reads_the_fresh_token(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("KITE_API_KEY=key1\nKITE_ACCESS_TOKEN=fresh-token\n")
    monkeypatch.setenv("KITE_API_KEY", "key1")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "stale-token")
    seen = {}

    class _SDK:
        def __init__(self, api_key):
            seen["key"] = api_key

        def set_access_token(self, token):
            seen["token"] = token

    monkeypatch.setattr(kiteconnect, "KiteConnect", _SDK)
    broker = kc.kite_broker_from_env(env)
    assert type(broker).__name__ == "KiteBroker"
    assert seen == {"key": "key1", "token": "fresh-token"}


def test_kite_broker_from_env_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
    assert kc.kite_broker_from_env(tmp_path / "missing.env") is None
