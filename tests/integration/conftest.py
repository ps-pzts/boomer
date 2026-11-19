"""Records every state-machine transition the integration suite fires (#88), for
test_zz_transition_coverage.py. Wraps `transition` where each module imported it."""

from __future__ import annotations

import pytest

HITS: set[tuple[str, str | None, str, str]] = set()  # (machine, from, trigger, to)


def _recording(orig):
    def transition(conn, spec, entity_id, trigger, **kw):
        row = conn.execute(
            f"SELECT {spec.state_column} FROM {spec.table} WHERE {spec.id_column}=?",  # noqa: S608
            (entity_id,),
        ).fetchone()
        result = orig(conn, spec, entity_id, trigger, **kw)
        HITS.add((spec.name, row[0] if row else None, trigger, result.state))
        return result

    return transition


@pytest.fixture(autouse=True, scope="session")
def _record_transitions():
    import executor.order_manager as om
    import executor.session_controller as sc
    import executor.trade_store as ts

    originals = [(m, m.transition) for m in (om, sc, ts)]
    for m, orig in originals:
        m.transition = _recording(orig)
    yield
    for m, orig in originals:
        m.transition = orig
