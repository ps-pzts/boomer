"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from tests.kite_sdk import fake_kite, verify


@pytest.fixture()
def kite_sdk():
    """Autospec'd kiteconnect.KiteConnect; fails the test on any off-contract response (#84)."""
    kite = fake_kite()
    yield kite
    verify(kite)
