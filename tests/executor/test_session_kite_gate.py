"""#85: the session cannot reach TRADING until today's Kite pre-flight and canary
passed; a failure halts it (`preflight_failed` / `canary_failed`) until an
operator re-run passes. Same one-day harness as test_session_controller."""

from __future__ import annotations

import json

import pytest

from executor import kite_checks as kc
from executor import session_machine as sm
from tests.executor.test_kite_checks import report
from tests.executor.test_session_controller import DAY, Day


@pytest.fixture()
def d(tmp_path) -> Day:
    return Day(tmp_path, kite_checks_passed=False)


def _rec(d: Day, kind: str, ok: bool, source: str = "scheduled") -> None:
    kc.record(d.h.db, DAY, report(kind, ok), source=source)


def _blocked_by(d: Day, trigger: str) -> list[str]:
    rows = d.h.db.execute(
        "SELECT payload FROM events WHERE kind='transition_blocked' AND name=?", (trigger,)
    ).fetchall()
    return [json.loads(r[0])["blocked_by"] for r in rows]


class TestGate:
    def test_cannot_trade_without_both_passes(self, d):
        d.at(9, 0)
        d.at(9, 1)
        assert d.at(9, 15) == sm.READY
        assert _blocked_by(d, "market_open") == ["preflight_pending"]
        _rec(d, "preflight", True)
        assert d.at(9, 16) == sm.READY
        assert _blocked_by(d, "market_open")[-1] == "canary_pending"
        assert not d.ctl.entries_allowed(DAY)
        _rec(d, "canary", True)
        assert d.at(9, 17) == sm.TRADING

    def test_unreadable_checks_fail_closed(self, d):
        d.h.db.execute("DROP TABLE kite_daily_checks")
        d.at(9, 0)
        d.at(9, 1)
        assert d.at(9, 15) == sm.READY
        assert _blocked_by(d, "market_open") == ["kite_checks_unavailable"]


class TestFailure:
    def test_preflight_failure_halts_before_the_open(self, d):
        _rec(d, "preflight", False)
        assert d.at(9, 0) == sm.HALTED
        assert d.row()["halt_reason"] == "preflight_failed"
        assert "Trading HALTED — preflight_failed" in d.h.critical_titles()
        assert d.transitions() == ["created", "checks_failed"]

    def test_canary_failure_halts_and_is_not_auto_resumable(self, d):
        _rec(d, "preflight", True)
        d.at(9, 0)
        d.at(9, 1)
        _rec(d, "canary", False)
        assert d.at(9, 16) == sm.HALTED
        assert d.row()["halt_reason"] == "canary_failed"
        assert d.at(9, 30) == sm.HALTED
        assert "recover" not in d.transitions()
        assert not d.ctl.entries_allowed(DAY)

    def test_operator_rerun_passing_resumes_via_reconcile(self, d):
        _rec(d, "preflight", True)
        _rec(d, "canary", False)
        d.at(9, 15)
        assert d.state() == sm.HALTED
        _rec(d, "canary", True, source="operator")
        assert d.at(9, 20) == sm.TRADING  # checks_passed -> RECONCILING -> clean -> TRADING
        assert d.transitions()[-2:] == ["checks_passed", "reconciled"]
        assert d.row()["halt_reason"] is None

    def test_rerun_passing_before_the_open_waits_for_it(self, d):
        _rec(d, "preflight", False)
        d.at(9, 0)
        _rec(d, "preflight", True, source="operator")
        _rec(d, "canary", True)
        assert d.at(9, 5) == sm.HALTED
        assert _blocked_by(d, "checks_passed") == ["before_open"]
        assert d.at(9, 15) == sm.TRADING

    def test_failed_rerun_while_trading_halts(self, d):
        _rec(d, "preflight", True)
        _rec(d, "canary", True)
        d.trading()
        _rec(d, "canary", False, source="operator")
        assert d.at(10, 0) == sm.HALTED
        assert d.row()["halt_reason"] == "canary_failed"

    def test_failure_after_cutoff_still_squares_off(self, d):
        _rec(d, "preflight", False)
        d.at(9, 0)
        assert d.at(14, 30) == sm.NO_NEW_ENTRIES
        assert d.at(15, 14) == sm.CLOSED  # nothing open: square_off + all_flat in one tick
        assert d.transitions()[-2:] == ["square_off", "all_flat"]
