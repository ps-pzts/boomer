"""Kite's order rules, as pure functions (#86).

Two kinds of refusal, as Kite has them:
  * input errors -> `kiteconnect.exceptions.InputException` raised by the call
    (bad params, price off the tick, stop trigger on the wrong side of the
    market; documented in tests/fixtures/kite/errors.json["input"])
  * RMS rejections -> the call returns an order_id and the order shows REJECTED
    with a Kite-like status_message (margin, circuit band, freeze quantity, MIS
    after the cutoff; orders.json has the margin and circuit messages)

Fill rules on one price event `p` (`continuous` = the price moved there through
every price in between, as within a backtest bar; a live tick or a bar open is a
jump, not continuous):
  SL-M  SELL triggers at p <= trigger; fills at the trigger if continuous, else
        at p when p is worse (a gap through the stop fills worse). BUY mirrors.
  SL    triggers the same way, then is a LIMIT at `price` (status OPEN).
  LIMIT marketable when placed / triggered -> fills at the market (p, or the
        trigger it just crossed); a resting limit the market reaches later fills
        at its limit price.
  MARKET fills at p.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from kiteconnect import exceptions as kex

ORDER_TYPES = frozenset({"MARKET", "LIMIT", "SL", "SL-M"})
STOP_TYPES = frozenset({"SL", "SL-M"})
PRICED_TYPES = frozenset({"LIMIT", "SL"})
PROTECTED_TYPES = frozenset({"MARKET", "SL-M"})
SIDES = frozenset({"BUY", "SELL"})
PRODUCTS = frozenset({"MIS", "CNC", "NRML"})
VALIDITIES = frozenset({"DAY", "IOC"})

WRONG_SIDE = {
    "SELL": "Trigger price for stoploss sell orders should be lower than the last traded price.",
    "BUY": "Trigger price for stoploss buy orders should be higher than the last traded price.",
}


def on_tick(value: float, tick: float) -> bool:
    if not value or tick <= 0:
        return True
    return Decimal(str(value)) % Decimal(str(tick)) == 0


def _fmt(x: float) -> str:
    return f"{x:.2f}"


def input_error(o: dict, tick: float, ltp: float | None) -> str | None:
    """Why Kite would refuse these order params outright, or None."""
    if o["order_type"] not in ORDER_TYPES:
        return f"Invalid `order_type`: {o['order_type']}."
    if o["transaction_type"] not in SIDES:
        return f"Invalid `transaction_type`: {o['transaction_type']}."
    if o["product"] not in PRODUCTS:
        return f"Invalid `product`: {o['product']}."
    if o["validity"] not in VALIDITIES:
        return f"Invalid `validity`: {o['validity']}."
    if not isinstance(o["quantity"], int) or isinstance(o["quantity"], bool) or o["quantity"] < 1:
        return "Invalid `quantity`."
    kind = o["order_type"]
    if kind in PRICED_TYPES and not o["price"] > 0:
        return "Invalid `price`."
    if kind in STOP_TYPES and not o["trigger_price"] > 0:
        return "Invalid `trigger_price`."
    if kind in PROTECTED_TYPES and o.get("market_protection") in (None, 0):
        return (
            "Market orders without market protection are not allowed via API. "
            "Please set market protection or use a Limit order."
        )
    for field in ("price", "trigger_price"):
        if o[field] and not on_tick(o[field], tick):
            return (
                f"Tick size for this script is {tick:.2f}. Kindly enter {field.replace('_', ' ')}"
                " in the multiple of tick size for this script"
            )
    return stop_side_error(o, ltp)


def stop_side_error(o: dict, ltp: float | None) -> str | None:
    """A stop whose trigger is already through the market, or an SL whose limit
    is on the wrong side of its own trigger."""
    if o["order_type"] not in STOP_TYPES:
        return None
    side, trig = o["transaction_type"], o["trigger_price"]
    if ltp is not None and ((side == "SELL" and trig >= ltp) or (side == "BUY" and trig <= ltp)):
        return WRONG_SIDE[side]
    if o["order_type"] == "SL":
        if side == "SELL" and o["price"] > trig:
            return "Trigger price for stoploss sell orders cannot be lower than the limit price."
        if side == "BUY" and o["price"] < trig:
            return "Trigger price for stoploss buy orders cannot be higher than the limit price."
    return None


def raise_input(message: str) -> None:
    raise kex.InputException(message)


def reference_price(o: dict, ltp: float | None) -> float | None:
    """The price RMS values the order at."""
    if o["order_type"] in PRICED_TYPES:
        return o["price"]
    if o["order_type"] == "SL-M":
        return o["trigger_price"]
    return ltp


def circuit_rejection(o: dict, ref: float | None, pct: float | None) -> str | None:
    if pct is None or ref is None or o["order_type"] not in PRICED_TYPES:
        return None
    lower, upper = ref * (1 - pct), ref * (1 + pct)
    if o["price"] < lower:
        return (
            f"Your order price is lower than the current lower circuit limit of {_fmt(lower)}. "
            "Place an order within the daily range."
        )
    if o["price"] > upper:
        return (
            f"Your order price is higher than the current upper circuit limit of {_fmt(upper)}. "
            "Place an order within the daily range."
        )
    return None


def margin_rejection(required: float, available: float, user: str) -> tuple[str, str] | None:
    if required <= available + 1e-9:
        return None
    return (
        f"Insufficient funds. Required margin is {_fmt(required)} but available margin is "
        f"{_fmt(available)}. Check the orderbook for open orders.",
        f"RMS:Margin Exceeds,Required:{_fmt(required)}, Available:{_fmt(available)} for entity "
        f"account-{user} across exchange across segment across product ",
    )


def freeze_rejection(qty: int, limit: int | None) -> str | None:
    if limit is None or qty <= limit:
        return None
    return f"Quantity exceeds the freeze limit of {limit}. Split the order and try again."


def mis_cutoff_rejection(product: str, now: datetime, cutoff: time) -> str | None:
    if product != "MIS" or now.time() < cutoff:
        return None
    return (
        f"MIS orders cannot be placed after {cutoff.strftime('%H:%M')}. "
        "Place a CNC order or try on the next trading day."
    )


# ── matching ──────────────────────────────────────────────────────────────────


def stop_crossed(side: str, trigger: float, p: float) -> bool:
    return p <= trigger if side == "SELL" else p >= trigger


def stop_fill_price(side: str, trigger: float, p: float, continuous: bool) -> float:
    if continuous:
        return trigger
    return min(trigger, p) if side == "SELL" else max(trigger, p)


def limit_marketable(side: str, limit: float, p: float) -> bool:
    return p <= limit if side == "BUY" else p >= limit
