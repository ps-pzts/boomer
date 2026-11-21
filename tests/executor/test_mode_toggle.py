"""#76: paper/live toggle, applied from the next session; today's mode is locked."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from db.migrations import run_migrations
from executor.live_readiness import Criterion, Report, Status
from executor.trading_mode import (
    LiveTradingAuthorisationStore,
    paper_record,
    request_mode,
    session_mode,
)
from tests.alerts.plain import plain

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


def _report(*statuses: Status) -> Report:
    names = ["paper_sessions", "zero_unprotected", "slippage", "expectancy", "b4_sebi"]
    return Report(
        [Criterion(n, st, f"{n} summary 7") for n, st in zip(names, statuses, strict=False)]
    )


@pytest.fixture()
def ready(monkeypatch):
    """Every Q5 criterion passes (the criteria themselves are tested in test_live_readiness)."""
    all_pass = _report(*[Status.PASS] * 5)
    monkeypatch.setattr("executor.trading_mode.build_report", lambda db: all_pass)


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

    def test_live_needs_confirmation_and_shows_the_paper_record(self, db, ready):
        r = _switch(db, "live")
        assert not r.ok and "0 sessions closed (criterion: ≥ 20)" in r.message
        assert not LiveTradingAuthorisationStore(db).is_enabled()

    def test_live_confirmed_before_todays_session_applies_today(self, db, ready):
        r = _switch(db, "live", confirmed=True)
        assert r.ok and "today's session" in r.message
        assert LiveTradingAuthorisationStore(db).is_enabled()
        assert session_mode(db, DAY, "live") == "live"

    def test_mid_session_switch_applies_next_session_only(self, db, ready):
        _session(db, DAY, "paper")
        r = _switch(db, "live", confirmed=True)
        assert r.ok and "next session" in r.message and "stays PAPER" in r.message
        assert session_mode(db, DAY, "live") == "paper"  # today is locked
        assert session_mode(db, date(2026, 11, 20), "live") == "live"

    def test_back_to_paper_needs_no_confirmation(self, db, ready):
        _switch(db, "live", confirmed=True)
        assert _switch(db, "paper", env="live").ok
        assert session_mode(db, DAY, "live") == "paper"

    @pytest.mark.parametrize("bad", [Status.FAIL, Status.INSUFFICIENT_DATA])
    @pytest.mark.parametrize("i", range(5))
    def test_live_refused_when_any_one_criterion_is_not_passing(self, db, monkeypatch, i, bad):
        sts = [Status.PASS] * 5
        sts[i] = bad
        monkeypatch.setattr("executor.trading_mode.build_report", lambda d: _report(*sts))
        r = _switch(db, "live", confirmed=True)
        name = _report(*sts).criteria[i].name
        assert not r.ok and f"{name}: {name} summary 7" in r.message
        assert r.message.count("summary") == 1  # only the failing criterion is listed
        assert not LiveTradingAuthorisationStore(db).is_enabled()

    def test_refused_even_before_confirmation_and_audited(self, db, caplog):
        with caplog.at_level("WARNING"):
            r = _switch(db, "live")  # real evidence, empty db: 0 of 20 sessions
        assert not r.ok and "0 closed paper sessions (need >= 20)" in r.message
        assert "Repeat with" not in r.message
        assert "live_switch_refused actor=op" in caplog.text

    def test_unreadable_evidence_fails_closed(self, db, monkeypatch):
        def boom(d):
            raise sqlite3.OperationalError("no such table: orders")

        monkeypatch.setattr("executor.trading_mode.build_report", boom)
        r = _switch(db, "live", confirmed=True)
        assert not r.ok and "could not read the readiness evidence" in r.message
        assert not LiveTradingAuthorisationStore(db).is_enabled()

    def test_paper_is_never_blocked_by_readiness(self, db, monkeypatch):
        LiveTradingAuthorisationStore(db).set_enabled(True, "op", "test")
        monkeypatch.setattr(
            "executor.trading_mode.build_report", lambda d: _report(*[Status.FAIL] * 5)
        )
        assert _switch(db, "paper", env="live").ok
        assert not LiveTradingAuthorisationStore(db).is_enabled()


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


def test_telegram_mode_command(db, monkeypatch, ready):
    from alerts.telegram_bot import handle_mode

    monkeypatch.setenv("BOOMER_TRADING_MODE", "live")
    assert "Today PAPER" in plain(handle_mode(db, "/mode"))
    assert "/mode live confirm" in handle_mode(db, "/mode live")
    assert handle_mode(db, "/mode live confirm").startswith("✅")
    assert "Next session LIVE" in plain(handle_mode(db, "/mode"))
