"""Kite GTT payloads (executor/brokers/kite_gtt.py, split out of KiteBroker in #86)."""

from __future__ import annotations

from kiteconnect import KiteConnect

from executor.brokers.kite_contract import check_shape
from executor.brokers.kite_gtt import gtt_modified_legs, gtt_place_params
from executor.models import GttRequest, GttType
from tests import kite_fixtures as kf


def test_single_leg_params():
    req = GttRequest("SAIL", "NSE", GttType.SINGLE, 10, trigger_price=90.0, limit_price=89.0)
    p = gtt_place_params(KiteConnect, req, 92.0)
    assert (p["trigger_type"], p["trigger_values"], p["last_price"]) == ("single", [90.0], 92.0)
    assert p["orders"] == [
        {
            "transaction_type": "SELL",
            "quantity": 10,
            "product": "CNC",
            "order_type": "LIMIT",
            "price": 89.0,
        }
    ]


def test_oco_params_stop_leg_first():
    req = GttRequest(
        "KPEL",
        "NSE",
        GttType.OCO,
        18,
        sl_trigger_price=338.05,
        sl_limit_price=337.35,
        target_trigger_price=439.45,
        target_limit_price=438.6,
    )
    p = gtt_place_params(KiteConnect, req, 360.0)
    assert p["trigger_type"] == "two-leg"
    assert p["trigger_values"] == [338.05, 439.45]
    assert [o["price"] for o in p["orders"]] == [337.35, 438.6]


def test_modified_legs_change_only_the_named_leg():
    current = kf.sdk("get_gtt")
    assert check_shape("gtt", current) == []
    values, orders = gtt_modified_legs(current, {"leg1": {"stopPrice": 340.0, "limitPrice": 339.3}})
    assert values == [340.0, 439.45]
    assert [o["price"] for o in orders] == [339.3, 438.6]
    assert set(orders[0]) == {"transaction_type", "quantity", "product", "order_type", "price"}


def test_no_changes_resends_the_trigger_as_is():
    values, orders = gtt_modified_legs(kf.sdk("get_gtt"), {})
    assert values == [338.05, 439.45]
    assert [o["price"] for o in orders] == [337.35, 438.6]
