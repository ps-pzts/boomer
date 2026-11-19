"""Session machine spec: one row per trading day (designs/state-machines.md §4, #63).

States, triggers and guards only. Guards are pure functions of the context the
SessionController builds once per tick (SM2): they never read the clock or the DB.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from statemachine import MachineSpec, Transition

PRE_OPEN = "pre_open"
CONNECTED = "connected"
READY = "ready"
TRADING = "trading"
HALTED = "halted"
RECONCILING = "reconciling"
NO_NEW_ENTRIES = "no_new_entries"
SQUARING_OFF = "squaring_off"
CLOSED = "closed"
ABANDONED = "abandoned"

# States that never reached TRADING. A session still here at square-off time is
# abandoned instead of squared off (no trade can exist).
NEVER_TRADED = frozenset({PRE_OPEN, CONNECTED, READY})
# Open trades are managed in every live state; only these allow new entries.
ENTRIES_ALLOWED = frozenset({TRADING})
TERMINAL = frozenset({CLOSED, ABANDONED})

# §4.3: halt reasons that may clear the same day (then RECONCILING, never straight
# back to TRADING). Everything else (loss breakers, cycle failures) holds for the
# rest of the day. An operator emergency stop is not a halt: it squares off at once (#67).
RESUMABLE = frozenset({"feed_stale", "breaker:nifty_move", "paused", "reconcile_mismatch"})

Ctx = Mapping[str, Any]


def _setup_ok(ctx: Ctx) -> str | None:
    return None if ctx.get("feed_connected") else "feed_not_connected"


def _watchlist_ready(ctx: Ctx) -> str | None:
    return None if ctx.get("watchlist_size", 0) > 0 else "watchlist_empty"


def _can_trade(ctx: Ctx) -> str | None:
    """Shared by market_open and reconciled: open, before the cutoff, nothing blocking."""
    if not ctx.get("market_open"):
        return "before_open"
    if not ctx.get("before_cutoff"):
        return "after_cutoff"
    return ctx.get("blocker")


def _recover(ctx: Ctx) -> str | None:
    reason = ctx.get("halt_reason")
    if reason not in RESUMABLE:
        return f"not_resumable:{reason}"
    if reason == "reconcile_mismatch":
        if not ctx.get("mismatch_acked"):
            return "awaiting_operator_ack"
    elif ctx.get("blocker"):
        return f"cause_active:{ctx['blocker']}"
    if not ctx.get("before_cutoff"):
        return "after_cutoff"
    return None


def _all_flat(ctx: Ctx) -> str | None:
    if ctx.get("active_trades", 0) > 0:
        return "trades_active"
    if ctx.get("broker_open_symbols"):
        return "broker_not_flat"
    return None


def _t(src: set[str], target: str, trigger: str, guard=None) -> Transition:
    return Transition(frozenset(src), target, trigger, guard)


_LIVE = {TRADING, HALTED, RECONCILING, NO_NEW_ENTRIES, READY, CONNECTED}

SESSION_SPEC = MachineSpec(
    name="session",
    table="trading_sessions",
    id_column="session_date",
    state_column="state",
    states=frozenset(
        {
            PRE_OPEN,
            CONNECTED,
            READY,
            TRADING,
            HALTED,
            RECONCILING,
            NO_NEW_ENTRIES,
            SQUARING_OFF,
            CLOSED,
            ABANDONED,
        }
    ),
    initial=PRE_OPEN,
    terminal=TERMINAL,
    transitions=(
        _t({PRE_OPEN}, CONNECTED, "setup_ok", _setup_ok),
        _t({CONNECTED}, READY, "watchlist_ready", _watchlist_ready),
        _t({READY}, TRADING, "market_open", _can_trade),
        _t({TRADING}, HALTED, "halt"),
        _t({HALTED}, RECONCILING, "recover", _recover),
        _t({RECONCILING}, TRADING, "reconciled", _can_trade),
        _t({RECONCILING}, HALTED, "reconcile_failed"),
        _t({CONNECTED, READY, TRADING}, RECONCILING, "restarted"),
        _t({TRADING, HALTED, RECONCILING}, NO_NEW_ENTRIES, "entry_cutoff"),
        _t(_LIVE, SQUARING_OFF, "square_off"),  # SM3: never guarded
        _t({SQUARING_OFF}, CLOSED, "all_flat", _all_flat),
        _t(set(NEVER_TRADED), ABANDONED, "abandon"),
    ),
    touch_column="updated_at",
)
