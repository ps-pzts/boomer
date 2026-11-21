"""Session machine spec and guards (designs/state-machines.md §4, #63)."""

from __future__ import annotations

import pytest

from executor import session_machine as sm
from executor.session_machine import SESSION_SPEC

_OK = {"blocker": None, "before_cutoff": True}


class TestSpec:
    def test_terminal_and_initial(self):
        assert SESSION_SPEC.initial == sm.PRE_OPEN
        assert SESSION_SPEC.terminal == {sm.CLOSED, sm.ABANDONED}

    @pytest.mark.parametrize(
        "state",
        [sm.CONNECTED, sm.READY, sm.TRADING, sm.HALTED, sm.RECONCILING, sm.NO_NEW_ENTRIES],
    )
    def test_square_off_never_guarded(self, state):
        """SM3: a halt, a reconcile, nothing can block the 15:14 square-off."""
        t = SESSION_SPEC.find(state, "square_off")
        assert t is not None and t.target == sm.SQUARING_OFF and t.guard is None

    def test_no_direct_resume_from_halted(self):
        """SM9 / #58: every same-day resume passes through RECONCILING."""
        assert {t.target for t in SESSION_SPEC.transitions if sm.HALTED in t.source} == {
            sm.RECONCILING,
            sm.NO_NEW_ENTRIES,
            sm.SQUARING_OFF,
        }

    def test_only_trading_allows_entries(self):
        assert {sm.TRADING} == sm.ENTRIES_ALLOWED

    def test_restart_from_live_setup_states_reconciles(self):
        for s in (sm.CONNECTED, sm.READY, sm.TRADING):
            assert SESSION_SPEC.find(s, "restarted").target == sm.RECONCILING
        assert SESSION_SPEC.find(sm.HALTED, "restarted") is None  # stays halted

    def test_abandon_only_if_never_traded(self):
        sources = {s for t in SESSION_SPEC.transitions if t.trigger == "abandon" for s in t.source}
        assert sources == sm.NEVER_TRADED

    def test_resumable_reasons_match_halt_policy(self):
        assert {
            "feed_stale",
            "breaker:nifty_move",
            "paused",
            "reconcile_mismatch",
        } == sm.RESUMABLE


def _guard(state: str, trigger: str):
    return SESSION_SPEC.find(state, trigger).guard


class TestGuards:
    def test_setup_needs_feed(self):
        g = _guard(sm.PRE_OPEN, "setup_ok")
        assert g({"feed_connected": False}) == "feed_not_connected"
        assert g({"feed_connected": True}) is None

    def test_watchlist_must_be_non_empty(self):
        g = _guard(sm.CONNECTED, "watchlist_ready")
        assert g({"watchlist_size": 0}) == "watchlist_empty"
        assert g({"watchlist_size": 50}) is None
        assert g({"watchlist_size": 1}) is None
        assert g({}) == "watchlist_empty"  # nothing recorded = nothing watched

    @pytest.mark.parametrize(
        "ctx, blocked",
        [
            ({"market_open": False, "before_cutoff": True}, "before_open"),
            ({"market_open": True, "before_cutoff": False}, "after_cutoff"),
            ({"market_open": True, "before_cutoff": True, "blocker": "feed_stale"}, "feed_stale"),
            ({"market_open": True, "before_cutoff": True, "blocker": None}, None),
        ],
    )
    def test_market_open_and_reconciled_share_the_trade_gate(self, ctx, blocked):
        assert _guard(sm.READY, "market_open")(ctx) == blocked
        assert _guard(sm.RECONCILING, "reconciled")(ctx) == blocked

    @pytest.mark.parametrize(
        "ctx, blocked",
        [
            (
                {"halt_reason": "breaker:intraday_daily_loss"},
                "not_resumable:breaker:intraday_daily_loss",
            ),
            ({"halt_reason": "emergency_stop"}, "not_resumable:emergency_stop"),
            (
                {"halt_reason": "feed_stale", "blocker": "feed_stale", "before_cutoff": True},
                "cause_active:feed_stale",
            ),
            (
                {"halt_reason": "feed_stale", "blocker": None, "before_cutoff": False},
                "after_cutoff",
            ),
            ({"halt_reason": "feed_stale", "blocker": None, "before_cutoff": True}, None),
            (
                {
                    "halt_reason": "reconcile_mismatch",
                    "mismatch_acked": False,
                    "before_cutoff": True,
                },
                "awaiting_operator_ack",
            ),
            (
                {
                    "halt_reason": "reconcile_mismatch",
                    "mismatch_acked": True,
                    "before_cutoff": True,
                },
                None,
            ),
        ],
    )
    def test_recover(self, ctx, blocked):
        assert _guard(sm.HALTED, "recover")(ctx) == blocked

    def test_all_flat_uses_broker_truth(self):
        g = _guard(sm.SQUARING_OFF, "all_flat")
        assert g({"active_trades": 1, "broker_open_symbols": []}) == "trades_active"
        assert g({"active_trades": 0, "broker_open_symbols": ["INFY"]}) == "broker_not_flat"
        assert g({"active_trades": 0, "broker_open_symbols": []}) is None
        assert g({}) is None  # nothing recorded = no trades, no broker positions


class TestKiteChecks:
    """#85: daily Kite pre-flight + canary gate the session."""

    def test_check_halts_are_not_auto_resumable(self):
        assert {"preflight_failed", "canary_failed"} == sm.KITE_CHECK_HALTS
        assert not sm.KITE_CHECK_HALTS & sm.RESUMABLE
        g = _guard(sm.HALTED, "recover")
        ctx = {"halt_reason": "canary_failed", "blocker": None, "before_cutoff": True}
        assert g(ctx) == "not_resumable:canary_failed"

    def test_a_failed_check_halts_from_every_pre_entry_state(self):
        for s in (sm.PRE_OPEN, sm.CONNECTED, sm.READY, sm.TRADING, sm.RECONCILING):
            t = SESSION_SPEC.find(s, "checks_failed")
            assert t is not None and t.target == sm.HALTED and t.guard is None
        assert SESSION_SPEC.find(sm.NO_NEW_ENTRIES, "checks_failed") is None

    def test_passing_rerun_resumes_through_reconciling(self):
        assert SESSION_SPEC.find(sm.HALTED, "checks_passed").target == sm.RECONCILING

    @pytest.mark.parametrize(
        "ctx, blocked",
        [
            ({"halt_reason": "feed_stale", "kite_checks": None}, "not_a_check_halt:feed_stale"),
            (
                {"halt_reason": "canary_failed", "kite_checks": "canary_failed"},
                "checks_not_passed:canary_failed",
            ),
            (
                {"halt_reason": "canary_failed", "kite_checks": None, "market_open": False},
                "before_open",
            ),
            (
                {
                    "halt_reason": "preflight_failed",
                    "kite_checks": None,
                    "market_open": True,
                    "before_cutoff": False,
                },
                "after_cutoff",
            ),
            (
                {
                    "halt_reason": "preflight_failed",
                    "kite_checks": None,
                    "market_open": True,
                    "before_cutoff": True,
                },
                None,
            ),
        ],
    )
    def test_checks_passed_guard(self, ctx, blocked):
        assert _guard(sm.HALTED, "checks_passed")(ctx) == blocked
