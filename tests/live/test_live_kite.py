"""Live Kite pre-flight and canary (#85). Opt-in: see tests/live/conftest.py.

The canary places two real qty-1 MIS orders that cannot fill and cancels them;
it only runs on a weekday 09:15-15:20 IST. Recordings go to
data/kite_recordings/ (or BOOMER_KITE_RECORDINGS_DIR) for use as fixtures.
"""

from __future__ import annotations

import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from executor.canary import CanaryConfig, run_canary
from executor.preflight import run_preflight

IST = ZoneInfo("Asia/Kolkata")
pytestmark = pytest.mark.live_kite


def test_live_preflight(live_broker):
    report = run_preflight(live_broker, None, symbol=os.environ.get("BOOMER_CANARY_SYMBOL", "INFY"))
    assert report.ok, report.failures()


def test_live_canary(live_broker):
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (time(9, 15) <= now.time() < time(15, 20)):
        pytest.skip(
            f"canary needs the normal market session (weekday 09:15-15:20 IST); now {now:%a %H:%M}"
        )
    config = CanaryConfig(
        symbol=os.environ.get("BOOMER_CANARY_SYMBOL", "INFY"),
        recordings_dir=os.environ.get("BOOMER_KITE_RECORDINGS_DIR", "data/kite_recordings"),
    )
    report = run_canary(live_broker, config)
    assert report.ok, report.failures()
