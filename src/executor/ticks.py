"""Order prices on the instrument's own tick (#75).

Kite rejects a price or trigger that isn't a multiple of the instrument's
`tick_size` (from Kite's instrument dump). Every order price is rounded here, at
the one choke point (OrderManager), in the direction that never hurts us:

  BUY  limit price  -> down  (never pay more than planned)
  SELL limit price  -> up    (never sell below the plan)
  any  trigger      -> up    (a SELL stop only gets tighter, never wider:
                              CLAUDE.md §10; a BUY breakout trigger only later)

Worked example, tick 0.10: BUY limit 1008.05 -> 1008.00; SELL stop trigger 978.05
-> 978.10; SELL target 1053.05 -> 1053.10.
"""

from __future__ import annotations

import dataclasses
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from executor.models import OrderRequest, OrderSide, OrderType

DEFAULT_TICK = 0.05


def round_to_tick(price: float, tick: float, up: bool) -> float:
    if price <= 0 or tick <= 0:
        return price
    t = Decimal(str(tick))
    steps = (Decimal(str(price)) / t).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR
    )
    return float(steps * t)


def on_tick(req: OrderRequest, tick: float) -> OrderRequest:
    """The request with price and trigger on the instrument's tick (see module doc)."""
    price = req.price
    if price > 0 and req.order_type in (OrderType.LIMIT, OrderType.SL_LIMIT):
        price = round_to_tick(price, tick, up=req.side == OrderSide.SELL)
    trigger = round_to_tick(req.trigger_price, tick, up=True) if req.trigger_price > 0 else 0.0
    if req.order_type == OrderType.SL_LIMIT and req.side == OrderSide.BUY and price < trigger:
        price = trigger  # a buy stop-limit's limit can't sit below its trigger
    return dataclasses.replace(req, price=price, trigger_price=trigger)


def is_on_tick(price: float, tick: float) -> bool:
    if price <= 0 or tick <= 0:
        return True
    return (Decimal(str(price)) % Decimal(str(tick))) == 0
