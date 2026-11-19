"""GTT triggers for KiteSim, shaped like Kite's `get_gtt()` (tests/fixtures/kite/gtt.json).

single    fires when the price crosses its one trigger value, from the side the
          last price was on when it was placed
two-leg   (OCO) fires leg 1 at price <= trigger_values[0] (stop), leg 2 at
          price >= trigger_values[1] (target)
A fired GTT is `triggered` and its leg is placed as a regular order; the leg's
`result` records that order (Kite's shape).
"""

from __future__ import annotations

import copy

from kiteconnect import exceptions as kex

from executor.brokers.kitesim.rules import on_tick

SINGLE, OCO = "single", "two-leg"
_LEG_KEYS = ("transaction_type", "quantity", "order_type", "product", "price")


def validate(
    trigger_type: str,
    trigger_values: list,
    last_price: float,
    orders: list[dict],
    tick: float,
) -> None:
    """Kite's GTT input rules; raises InputException."""
    if trigger_type not in (SINGLE, OCO):
        raise kex.InputException(f"Invalid trigger type: {trigger_type}.")
    want = 1 if trigger_type == SINGLE else 2
    if not isinstance(trigger_values, list) or len(trigger_values) != want:
        raise kex.InputException(f"invalid `trigger_values` for {trigger_type} order type")
    if len(orders) != want:
        raise kex.InputException(f"A {trigger_type} trigger needs {want} order(s).")
    for o in orders:
        for k in _LEG_KEYS:
            if k not in o:
                raise kex.InputException(f"`{k}` missing inside orders")
        if not on_tick(float(o["price"]), tick):
            raise kex.InputException(
                f"Tick size for this script is {tick:.2f}. Kindly enter price in the multiple "
                "of tick size for this script"
            )
    for v in trigger_values:
        if not on_tick(float(v), tick):
            raise kex.InputException(f"Trigger value {v} is not a multiple of tick {tick:.2f}.")
    if trigger_type == OCO and not trigger_values[0] < last_price < trigger_values[1]:
        raise kex.InputException(
            "The stoploss trigger should be below and the target trigger above the last price."
        )
    if trigger_type == SINGLE and trigger_values[0] == last_price:
        raise kex.InputException("Trigger price should not be equal to the last price.")


def build(
    gtt_id: int,
    user_id: str,
    trigger_type: str,
    symbol: str,
    exchange: str,
    token: int,
    trigger_values: list,
    last_price: float,
    orders: list[dict],
    now: str,
    expires: str,
) -> dict:
    return {
        "id": gtt_id,
        "user_id": user_id,
        "parent_trigger": None,
        "type": trigger_type,
        "created_at": now,
        "updated_at": now,
        "expires_at": expires,
        "status": "active",
        "condition": {
            "exchange": exchange,
            "last_price": float(last_price),
            "tradingsymbol": symbol,
            "trigger_values": [float(v) for v in trigger_values],
            "instrument_token": token,
        },
        "orders": [
            {
                "exchange": exchange,
                "tradingsymbol": symbol,
                "product": o["product"],
                "order_type": o["order_type"],
                "transaction_type": o["transaction_type"],
                "quantity": int(o["quantity"]),
                "price": float(o["price"]),
                "result": None,
            }
            for o in orders
        ],
        "meta": None,
    }


def fired_leg(gtt: dict, p: float) -> int | None:
    """Index of the leg this price fires, or None."""
    if gtt["status"] != "active":
        return None
    values = gtt["condition"]["trigger_values"]
    if gtt["type"] == OCO:
        if p <= values[0]:
            return 0
        if p >= values[1]:
            return 1
        return None
    trig = values[0]
    from_above = gtt["condition"]["last_price"] > trig
    return 0 if (p <= trig if from_above else p >= trig) else None


def result(leg: dict, order_id: str, status: str, reason: str, now: str, user: str) -> dict:
    return {
        "account_id": user,
        "exchange": leg["exchange"],
        "tradingsymbol": leg["tradingsymbol"],
        "validity": "DAY",
        "product": leg["product"],
        "order_type": leg["order_type"],
        "transaction_type": leg["transaction_type"],
        "quantity": leg["quantity"],
        "price": leg["price"],
        "meta": "",
        "timestamp": now,
        "triggered_at": now,
        "order_result": {"status": status, "order_id": order_id, "rejection_reason": reason},
    }


def view(gtt: dict) -> dict:
    return copy.deepcopy(gtt)
