"""#76: paper/live toggle, applied from the next session; today's mode is locked."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from db.migrations import run_migrations
from executor.trading_mode import (
    LiveTradingAuthorisationStore,
    paper_record,
    request_mode,
    session_mode,
)

DAY = date(2026, 11, 19)
_MIGRATIONS = Path(__file__).parents[2] / "migrations"


@pytest.fixture()
def db(tmp_path) -> str:
    p = str(tmp_path / "m.db")
    run_migrations(p, _MIGRATIONS)
    return p


def _session(db: str, day: date, mode: str, state: str = "trading") -> None:
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO trading_sessions (session_date, mode, state, created_at, updated_at)"
            " VALUES (?, ?, ?, 'c', 'u')",
            (day.isoformat(), mode, state),
        )


def _switch(db, target, env="live", confirmed=False, today=DAY):
    return request_mode(
        db, target, actor="op", reason="test", env_value=env, confirmed=confirmed, today=today
    )


class TestRequestMode:
    def test_unknown_target(self, db):
        assert not _switch(db, "yolo").ok

    def test_live_refused_when_server_not_armed(self, db):
        r = _switch(db, "live", env="paper", confirmed=True)
        assert not r.ok and "not armed" in r.message
        assert not LiveTradingAuthorisationStore(db).is_enabled()

    def test_live_needs_confirmation_and_shows_the_paper_record(self, db):
        r = _switch(db, "live")
        assert not r.ok and "0 sessions closed (criterion: ≥ 20)" in r.message
        assert not LiveTradingAuthorisationStore(db).is_enabled()

    def test_live_confirmed_before_todays_session_applies_today(self, db):
        r = _switch(db, "live", confirmed=True)
        assert r.ok and "today's session" in r.message
        assert LiveTradingAuthorisationStore(db).is_enabled()
        assert session_mode(db, DAY, "live") == "live"

    def test_mid_session_switch_applies_next_session_only(self, db):
        _session(db, DAY, "paper")
        r = _switch(db, "live", confirmed=True)
        assert r.ok and "next session" in r.message and "stays PAPER" in r.message
        assert session_mode(db, DAY, "live") == "paper"  # today is locked
        assert session_mode(db, date(2026, 11, 20), "live") == "live"

    def test_back_to_paper_needs_no_confirmation(self, db):
        _switch(db, "live", confirmed=True)
        assert _switch(db, "paper", env="live").ok
        assert session_mode(db, DAY, "live") == "paper"


def test_paper_record_counts_only_paper(db):
    _session(db, date(2026, 11, 17), "paper", "closed")
    _session(db, date(2026, 11, 18), "live", "closed")
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        for i, (day, pnl, reason) in enumerate(
            [
                ("2026-11-17", 40.0, "target_hit"),
                ("2026-11-17", -30.0, "stop_cancelled"),
                ("2026-11-18", 99.0, "target_hit"),
            ]
        ):
            c.execute(
                "INSERT INTO intraday_brackets (bracket_id, session_date, symbol, exchange,"
                " planned_quantity, entry_price, stop_price, target_price, atr,"
                " entry_order_id, entry_expires_at, state, exit_reason, realised_pnl,"
                " created_at, updated_at) VALUES (?, ?, 'X', 'NSE', 1, 100, 98, 104, 2, 'o',"
                " 'e', 'closed', ?, ?, 'c', 'u')",
                (f"b{i}", day, reason, pnl),
            )
    assert paper_record(db) == {
        "paper_sessions_closed": 1,
        "paper_trades": 2,
        "paper_realised_pnl": 10.0,
        "unprotected_exits": 1,
    }


def test_telegram_mode_command(db, monkeypatch):
    from alerts.telegram_bot import handle_mode

    monkeypatch.setenv("BOOMER_TRADING_MODE", "live")
    assert "Today: PAPER" in handle_mode(db, "/mode")
    assert "/mode live confirm" in handle_mode(db, "/mode live")
    assert handle_mode(db, "/mode live confirm").startswith("✅")
    assert "Next session: LIVE" in handle_mode(db, "/mode")
