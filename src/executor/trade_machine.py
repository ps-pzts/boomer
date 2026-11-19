"""Trade machine spec (designs/state-machines.md §5, #45 / SM-2).

    entry_pending --entry_filled--> protecting --stop_confirmed--> open
    entry_pending --entry_expired | entry_rejected | square_off--> cancelled
    protecting    --stop_failed | square_off-->                     exiting
    open          --leg_filled | stop_lost | square_off-->          exiting
    exiting       --corrective_order | exit_failed-->               exiting
    exiting       --flat-->                                         closed

PROTECTING is the window between the entry fill and a confirmed resting stop:
the position exists with no confirmed protection. EXITING means "bring the net
position to zero": every exit, including a normal target or stop hit, passes
through it, so the event log shows the exit leg and the final settlement
separately.
"""

from __future__ import annotations

from statemachine import MachineSpec, Transition


def _t(source: set[str], target: str, trigger: str) -> Transition:
    return Transition(frozenset(source), target, trigger)


TRADE_SPEC = MachineSpec(
    name="trade",
    table="intraday_brackets",
    id_column="bracket_id",
    state_column="state",
    states=frozenset({"entry_pending", "protecting", "open", "exiting", "closed", "cancelled"}),
    initial="entry_pending",
    terminal=frozenset({"closed", "cancelled"}),
    transitions=(
        _t({"entry_pending"}, "protecting", "entry_filled"),
        _t({"entry_pending"}, "cancelled", "entry_expired"),
        _t({"entry_pending"}, "cancelled", "entry_rejected"),
        _t({"entry_pending"}, "cancelled", "square_off"),
        _t({"protecting"}, "open", "stop_confirmed"),
        _t({"protecting"}, "exiting", "stop_failed"),
        _t({"protecting", "open"}, "exiting", "square_off"),
        _t({"open"}, "exiting", "leg_filled"),
        _t({"open"}, "exiting", "stop_lost"),
        _t({"exiting"}, "exiting", "corrective_order"),
        _t({"exiting"}, "exiting", "exit_failed"),
        _t({"exiting"}, "closed", "flat"),
    ),
    touch_column="updated_at",
)
