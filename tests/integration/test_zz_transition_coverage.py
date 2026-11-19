"""The gate (#88): every transition of the Session, Trade and Order machines is fired by
the integration suite, or is listed in spec_only.py against an open issue.

Runs last (zz) and only when the whole suite ran, since it reads what the other
tests fired. A new transition added to a spec without a scenario fails here.
"""

from __future__ import annotations

import pytest

from executor.order_machine import ORDER_SPEC
from executor.session_machine import SESSION_SPEC
from executor.trade_machine import TRADE_SPEC
from tests.integration.conftest import HITS
from tests.integration.spec_only import SPEC_ONLY

_MIN_MODULES = 5


def _transitions(spec):
    return {(spec.name, s, t.trigger, t.target) for t in spec.transitions for s in t.source}


def test_every_transition_is_fired_or_explained(request):
    modules = {
        item.module.__name__ for item in request.session.items if "integration" in item.nodeid
    }
    if len(modules) < _MIN_MODULES:
        pytest.skip("run the whole tests/integration directory for the coverage gate")
    allowed = set(SPEC_ONLY)
    spec = set()
    for s in (SESSION_SPEC, TRADE_SPEC, ORDER_SPEC):
        spec |= _transitions(s)
    stale = allowed - spec
    assert not stale, f"spec_only.py lists transitions the specs no longer have: {sorted(stale)}"
    fired_but_listed = allowed & HITS
    assert not fired_but_listed, (
        f"now reachable, drop from spec_only.py: {sorted(fired_but_listed)}"
    )
    missed = sorted(spec - HITS - allowed)
    assert not missed, "transitions no integration test fires:\n  " + "\n  ".join(map(str, missed))
