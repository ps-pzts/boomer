"""#84: the Kite SDK contract. Every fixture passes; broken variants are caught."""

from __future__ import annotations

import datetime as dt

import kiteconnect.exceptions as kex
import pytest

from executor.brokers import kite_contract as kc
from executor.brokers.kite_broker import _KITE_FINAL, map_kite_status
from executor.brokers.kite_contract import check_shape
from executor.models import OrderStatus
from tests import kite_fixtures as kf

# (contract kind, value) for every fixture, through the real SDK where one parses it.
_GOOD = {
    "orders": lambda: kf.sdk("orders"),
    "order": lambda: kf.sdk("orders")[0],
    "order_postback": kf.postback,
    "positions": lambda: kf.sdk("positions"),
    "position": lambda: kf.sdk("positions")["net"][0],
    "holdings": lambda: kf.sdk("holdings"),
    "holding": lambda: kf.sdk("holdings")[0],
    "margins_equity": lambda: kf.sdk("margins"),
    "ltp": lambda: kf.sdk("ltp"),
    "instruments": lambda: kf.sdk("instruments"),
    "instrument": lambda: kf.sdk("instruments")[0],
    "gtt": lambda: kf.sdk("get_gtt"),
    "gtts": lambda: kf.sdk("get_gtts"),
    "gtt_ack": lambda: kf.sdk("place_gtt"),
    "place_order": lambda: kf.sdk("place_order"),
    "session": lambda: kf.sdk("generate_session"),
    "historical": lambda: kf.sdk("historical_data"),
}


@pytest.mark.parametrize("kind", sorted(_GOOD))
def test_every_fixture_matches_the_contract(kind):
    assert check_shape(kind, _GOOD[kind]()) == []


def test_every_contract_kind_has_a_fixture():
    assert set(kc.KINDS) == set(_GOOD)


@pytest.mark.parametrize("order", kf.sdk("orders"), ids=lambda o: o["status"])
def test_every_lifecycle_order_matches(order):
    assert check_shape("order", order) == []


def test_lifecycle_covers_the_statuses_we_trade_on():
    seen = {o["status"] for o in kf.sdk("orders")}
    want = {"COMPLETE", "TRIGGER PENDING", "OPEN", "MODIFY PENDING", "CANCEL PENDING"}
    assert want | {"CANCELLED", "REJECTED"} <= seen
    assert any(0 < o["filled_quantity"] < o["quantity"] for o in kf.sdk("orders"))


def test_raw_wire_orders_match_too():
    """The canary may store JSON (timestamps as strings), not SDK objects."""
    assert check_shape("orders", kf.data("orders.json")) == []


def test_sdk_parses_order_timestamps():
    assert isinstance(kf.sdk("orders")[0]["order_timestamp"], dt.datetime)


def test_place_order_is_the_bare_id_string():
    """The #73 bug: KiteConnect.place_order returns '2406...', not {'order_id': ...}."""
    assert kf.sdk("place_order") == "240611000123401"
    assert check_shape("place_order", {"order_id": "240611000123401"}) != []


# ── broken variants ───────────────────────────────────────────────────────────


def _order(**changes):
    o = kf.sdk("orders")[0]
    o.update(changes)
    return o


def test_missing_key_is_reported():
    o = kf.sdk("orders")[0]
    del o["filled_quantity"]
    assert check_shape("order", o) == ["order.filled_quantity: missing"]


def test_wrong_type_is_reported():
    problems = check_shape("order", _order(order_id=240611000123401))
    assert problems == ["order.order_id: expected str, got int"]


def test_bool_is_not_a_number():
    assert check_shape("order", _order(quantity=True)) == ["order.quantity: expected int, got bool"]


def test_unknown_order_status_is_reported():
    problems = check_shape("order", _order(status="SOMETHING NEW"))
    assert problems == ["order.status: 'SOMETHING NEW' not in Kite's vocabulary"]


def test_unknown_enum_values_are_reported():
    assert check_shape("order", _order(product="XYZ")) == [
        "order.product: 'XYZ' not in Kite's vocabulary"
    ]


def test_optional_key_may_be_absent_but_is_typed_when_present():
    o = kf.sdk("orders")[0]
    del o["tags"]
    assert check_shape("order", o) == []
    o["tags"] = "abc"
    assert check_shape("order", o) == ["order.tags: expected list | None, got str"]


def test_nested_margin_fields_are_checked():
    m = kf.sdk("margins")
    del m["available"]["cash"]
    m["utilised"]["debits"] = "145706.55"
    assert check_shape("margins_equity", m) == [
        "margins_equity.available.cash: missing",
        "margins_equity.utilised.debits: expected int | float, got str",
    ]


def test_positions_need_both_buckets_and_check_each_row():
    p = kf.sdk("positions")
    del p["day"]
    p["net"][1]["quantity"] = 1.5
    assert check_shape("positions", p) == [
        "positions.net[1].quantity: expected int, got float",
        "positions.day: missing",
    ]


def test_ltp_keys_must_be_exchange_colon_symbol():
    assert check_shape("ltp", {"INFY": {"instrument_token": 1, "last_price": 1.0}}) == [
        "ltp: key 'INFY' is not EXCHANGE:SYMBOL"
    ]
    assert check_shape("ltp", {"NSE:INFY": {"instrument_token": 1}}) == [
        "ltp['NSE:INFY'].last_price: missing"
    ]


def test_gtt_nested_condition_and_legs_are_checked():
    g = kf.sdk("get_gtt")
    g["condition"]["trigger_values"] = 338.05
    g["orders"][0].pop("price")
    assert check_shape("gtt", g) == [
        "gtt.condition.trigger_values: expected list, got float",
        "gtt.orders[0].price: missing",
    ]


def test_non_object_payload_is_reported():
    assert check_shape("order", []) == ["order: expected object, got list"]
    assert check_shape("orders", {}) == ["orders: expected list, got dict"]


def test_unknown_kind_raises():
    with pytest.raises(KeyError, match="nope"):
        check_shape("nope", {})


# ── vocabulary ────────────────────────────────────────────────────────────────


def test_final_statuses_match_kite_broker():
    assert set(_KITE_FINAL) == kc.FINAL_ORDER_STATUSES


@pytest.mark.parametrize("status", sorted(kc.ORDER_STATUSES))
def test_every_known_status_is_handled_without_warning(status, caplog):
    map_kite_status(status, "SL-M", 0, 10)
    assert "kite_unknown_order_status" not in caplog.text


@pytest.mark.parametrize("status", sorted(kc.FINAL_ORDER_STATUSES))
def test_final_statuses_are_terminal(status):
    assert map_kite_status(status, "LIMIT", 0, 10) in {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }


@pytest.mark.parametrize("kind", sorted(kc.EXCEPTIONS))
def test_documented_errors_raise_the_contract_exceptions(kind):
    exc = kf.sdk_error(kind)
    assert type(exc) is kc.EXCEPTIONS[kind]
    assert isinstance(exc, kex.KiteException)
