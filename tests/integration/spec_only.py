"""Spec transitions the integration suite cannot fire because no code path emits them.

Each entry is (machine, from_state, trigger, to_state) and must cite an open issue.
Remove an entry the day its issue is fixed: the coverage gate then demands a test.
"""

from __future__ import annotations

_ERROR_FROM = (
    "cancelled",
    "created",
    "expired",
    "filled",
    "partial",
    "pending",
    "rejected",
    "triggered",
)

SPEC_ONLY: dict[tuple[str, str, str, str], str] = {
    # #100: only SUBMITTING -> ERROR ever fires; the rest of the spec is dead.
    **{("order", s, "error", "error"): "#100" for s in _ERROR_FROM},
    ("order", "pending", "expired", "expired"): "#100",
    ("order", "submitting", "rejected", "rejected"): "#100",
}
