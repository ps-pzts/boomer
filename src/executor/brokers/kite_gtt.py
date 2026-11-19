"""Kite GTT payloads for KiteBroker (split out of kite_broker.py, #86)."""

from __future__ import annotations

from executor.models import GttRequest, GttType


def _leg(quantity: int, price: float | None) -> dict:
    return {
        "transaction_type": "SELL",
        "quantity": quantity,
        "product": "CNC",
        "order_type": "LIMIT",
        "price": price,
    }


def gtt_place_params(kite: object, request: GttRequest, last_price: float) -> dict:
    """kite.place_gtt(**params) for a single-leg or OCO (stop + target) SELL GTT."""
    if request.gtt_type == GttType.SINGLE:
        return {
            "trigger_type": kite.GTT_TYPE_SINGLE,  # type: ignore[attr-defined]
            "tradingsymbol": request.symbol,
            "exchange": request.exchange,
            "trigger_values": [request.trigger_price],
            "last_price": last_price,
            "orders": [_leg(request.quantity, request.limit_price)],
        }
    return {
        "trigger_type": kite.GTT_TYPE_OCO,  # type: ignore[attr-defined]
        "tradingsymbol": request.symbol,
        "exchange": request.exchange,
        "trigger_values": [request.sl_trigger_price, request.target_trigger_price],
        "last_price": last_price,
        "orders": [
            _leg(request.quantity, request.sl_limit_price),
            _leg(request.quantity, request.target_limit_price),
        ],
    }


def gtt_modified_legs(current: dict, changes: dict) -> tuple[list, list[dict]]:
    """The full trigger_values and orders of `current` (a get_gtt payload) with the
    `{"legN": {"stopPrice", "limitPrice"}}` changes applied (#84: Kite's
    modify_gtt replaces the whole trigger)."""
    trigger_values = list(current["condition"]["trigger_values"])
    orders = [
        {k: o[k] for k in ("transaction_type", "quantity", "product", "order_type", "price")}
        for o in current["orders"]
    ]
    for i in range(len(orders)):
        leg = changes.get(f"leg{i + 1}")
        if leg:
            trigger_values[i] = leg["stopPrice"]
            orders[i]["price"] = leg["limitPrice"]
    return trigger_values, orders
