"""Live Kite tests (#85): opt-in only.

Never run by default: pyproject's addopts deselects the `live_kite` marker, and
even when selected every test here is skipped unless BOOMER_LIVE_KITE=1 and a
Kite token is available (environment or the repo's .env). Run with:

    BOOMER_LIVE_KITE=1 pytest tests/live -m live_kite
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests/live/" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(pytest.mark.live_kite)


@pytest.fixture(scope="session")
def live_broker():
    if os.environ.get("BOOMER_LIVE_KITE") != "1":
        pytest.skip("live Kite tests are opt-in: set BOOMER_LIVE_KITE=1")
    from executor.kite_checks import kite_broker_from_env

    broker = kite_broker_from_env()
    if broker is None:
        pytest.skip("no Kite token: set KITE_API_KEY and KITE_ACCESS_TOKEN (or .env)")
    return broker
