"""#100: a definite Kite refusal is a BrokerRejected; an unknown outcome is not."""

from __future__ import annotations

import kiteconnect.exceptions as kex
import pytest

from executor.brokers.kite_broker import _refused
from executor.models import BrokerRejected


@pytest.mark.parametrize("cls", [kex.InputException, kex.OrderException])
def test_refusal_is_both_the_kite_error_and_a_broker_rejected(cls):
    err = _refused(cls("RMS: margin"))
    assert isinstance(err, BrokerRejected) and isinstance(err, cls)
    assert err.args == ("RMS: margin",)


def test_network_failure_is_not_a_refusal():
    """A timeout may have placed the order (#97), so it must stay an unknown outcome."""
    assert not isinstance(kex.NetworkException("timed out"), BrokerRejected)
