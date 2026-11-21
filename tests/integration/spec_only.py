"""Spec transitions the integration suite cannot fire because no code path emits them (none now).

Each entry is (machine, from_state, trigger, to_state) and must cite an open issue.
Remove an entry the day its issue is fixed: the coverage gate then demands a test.
"""

from __future__ import annotations

SPEC_ONLY: dict[tuple[str, str, str, str], str] = {}  # empty since #100 trimmed the Order spec
