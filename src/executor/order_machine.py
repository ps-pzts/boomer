"""Order machine spec (designs/state-machines.md §6, #56).

Built from ALLOWED_TRANSITIONS so there is one source of truth for legal order
status changes. The trigger is the target status (an order transition is
"the broker now says X"). FILLED, CANCELLED, REJECTED and ERROR are final (#100:
the spec lists only transitions a real order can take).

Note (design §6.2): Kite's "TRIGGER PENDING" (a stop still waiting) maps to our
TRIGGERED, so PENDING and TRIGGERED are both resting states.
"""

from __future__ import annotations

from executor.models import ALLOWED_TRANSITIONS, OrderStatus
from statemachine import MachineSpec, Transition

ORDER_SPEC = MachineSpec(
    name="order",
    table="orders",
    id_column="order_id",
    state_column="status",
    states=frozenset(s.value for s in OrderStatus),
    initial=OrderStatus.CREATED.value,
    terminal=frozenset(
        s.value
        for s in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.ERROR,
        )
    ),
    transitions=tuple(
        Transition(source=frozenset({src.value}), target=dst.value, trigger=dst.value)
        for src, dsts in ALLOWED_TRANSITIONS.items()
        for dst in sorted(dsts)
    ),
    touch_column="updated_at",
)
