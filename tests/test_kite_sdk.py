"""#84: the autospec'd Kite SDK fake refuses shapes Kite never produces."""

from __future__ import annotations

import contextlib

import pytest
import requests
from kiteconnect import KiteConnect
from kiteconnect.exceptions import PermissionException

from tests import kite_fixtures as kf
from tests.kite_sdk import ContractViolation, fake_kite, respond, verify


def test_is_autospecced_on_the_real_sdk():
    kite = fake_kite()
    with pytest.raises(AttributeError):
        kite.no_such_method  # noqa: B018
    with pytest.raises(TypeError):
        kite.place_order(variety="regular", leg1={})  # unknown kwarg, missing required


def test_keeps_the_sdk_constants():
    kite = fake_kite()
    assert (kite.VARIETY_REGULAR, kite.GTT_TYPE_OCO) == (
        KiteConnect.VARIETY_REGULAR,
        KiteConnect.GTT_TYPE_OCO,
    )


def test_fixture_response_is_returned():
    kite = fake_kite()
    respond(kite, "orders", kf.sdk("orders"))
    assert kite.orders() == kf.sdk("orders")
    verify(kite)


def test_return_value_set_directly_is_checked_too():
    kite = fake_kite()
    kite.place_order.return_value = {"order_id": "1"}  # the #73 wrong assumption
    with pytest.raises(ContractViolation, match="place_order"):
        kite.place_order(
            variety="regular",
            exchange="NSE",
            tradingsymbol="INFY",
            transaction_type="BUY",
            quantity=1,
            product="MIS",
            order_type="MARKET",
        )


def test_wrong_shape_fails_even_if_the_caller_swallows_it():
    kite = fake_kite()
    respond(kite, "ltp", {"NSE:INFY": {"last_price": "1510"}})
    with contextlib.suppress(Exception):  # e.g. KiteBroker.get_ltp's fallback chain
        kite.ltp("NSE:INFY")
    with pytest.raises(ContractViolation, match="ltp"):
        verify(kite)


def test_unconfigured_call_is_a_violation():
    kite = fake_kite()
    with pytest.raises(ContractViolation, match="no response"):
        kite.holdings()


def test_sequence_then_last_repeats():
    kite = fake_kite()
    first = kf.orders_where(status="TRIGGER PENDING")
    last = kf.orders_where(status="COMPLETE")
    respond(kite, "orders", first, last)
    assert [kite.orders(), kite.orders(), kite.orders()] == [first, last, last]


def test_exceptions_must_be_ones_the_sdk_raises():
    kite = fake_kite()
    respond(kite, "ltp", kf.sdk_error("permission"))
    with pytest.raises(PermissionException):
        kite.ltp("NSE:INFY")
    respond(kite, "ltp", requests.ConnectionError("reset"))
    with pytest.raises(requests.ConnectionError):
        kite.ltp("NSE:INFY")
    verify(kite)
    respond(kite, "ltp", Exception("made up"))
    with pytest.raises(ContractViolation, match="never raises"):
        kite.ltp("NSE:INFY")


def test_callable_response_is_checked():
    kite = fake_kite()
    respond(kite, "orders", lambda: [{"order_id": 1}])
    with pytest.raises(ContractViolation, match="orders"):
        kite.orders()


def test_overriding_side_effect_directly_is_caught():
    kite = fake_kite()
    kite.orders.side_effect = lambda: []
    with pytest.raises(ContractViolation, match="respond"):
        verify(kite)


def test_margins_checked_against_the_equity_segment():
    kite = fake_kite()
    respond(kite, "margins", kf.sdk("margins"))
    assert kite.margins(segment="equity")["available"]["cash"] == 245431.6
    verify(kite)
