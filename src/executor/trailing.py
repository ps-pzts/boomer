"""Trailing stop for OPEN intraday trades (#71, design L11).

Operator rule (2026-11-19): once a trade is up 1R, move the stop to breakeven;
after that trail it 1 x ATR below the highest price seen since entry. The fixed
target stays. Stops only ever move UP (CLAUDE.md §10: never widen).

Worked example (fill 100, initial stop 98 -> R = 2, ATR 2):
  high 101.95             -> nothing (below entry + 1R = 102)
  high 102.00             -> max(100, 102 - 2) = 100.00   breakeven
  high 103.50             -> max(100, 101.5)   = 101.50   trail (moved 1.5 >= 0.25 x ATR)
  high 103.60             -> 101.60: moved only 0.10 < 0.50, skipped (modify budget)
  LTP 101.40, candidate 101.50 -> skipped: a SELL stop must sit below the last price

High-water is the LTP sampled on each minute tick. It can miss an intrabar high, which
only makes the trail looser, never tighter.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

logger = logging.getLogger(__name__)

TRAIL_ATR = 1.0  # trail distance below the high, in ATRs
MIN_STEP_ATR = 0.25  # past breakeven, move only by at least this many ATRs
MAX_MOVES = 20  # Kite allows ~25 modifications per order; keep headroom
TICK = Decimal("0.05")


@dataclass(frozen=True)
class StopMove:
    new_stop: float | None  # None = leave the stop where it is
    rule: str  # why (moved: breakeven / trail; held: the blocking reason)


def _floor_tick(x: float) -> float:
    return float((Decimal(str(x)) / TICK).to_integral_value(rounding=ROUND_FLOOR) * TICK)


def next_stop(
    *,
    entry: float,
    initial_stop: float,
    current_stop: float,
    high_water: float,
    atr: float,
    ltp: float,
    moves: int,
) -> StopMove:
    """Pure: where the stop should go now, or why it stays."""
    risk = entry - initial_stop
    if risk <= 0 or atr <= 0:
        return StopMove(None, "invalid_inputs")
    if high_water < entry + risk:
        return StopMove(None, "below_1r")
    trail = high_water - TRAIL_ATR * atr
    candidate, rule = (trail, "trail") if trail > entry else (entry, "breakeven")
    candidate = _floor_tick(candidate)
    if candidate <= current_stop:
        return StopMove(None, "not_higher")
    if current_stop >= entry and candidate - current_stop < MIN_STEP_ATR * atr:
        return StopMove(None, "step_too_small")
    if candidate >= ltp:
        return StopMove(None, "at_or_above_market")
    if moves >= MAX_MOVES:
        return StopMove(None, "max_moves")
    return StopMove(candidate, rule)


class Trailer:
    """Applies next_stop() to one OPEN bracket: broker modify first, then the
    `stop_raised` transition with the new columns, atomically."""

    def __init__(self, store: object, order_manager: object, price: Callable[[str], float | None]):
        self._store = store
        self._om = order_manager
        self._price = price

    def step(self, b: sqlite3.Row, entry_fill: float) -> None:
        ltp = self._price(b["symbol"])
        if ltp is None:
            return
        high = max(ltp, b["high_water"] or entry_fill)
        current = b["trail_stop"] or b["stop_price"]
        move = next_stop(
            entry=entry_fill,
            initial_stop=b["stop_price"],
            current_stop=current,
            high_water=high,
            atr=b["atr"],
            ltp=ltp,
            moves=b["stop_moves"],
        )
        if move.new_stop is None:
            if high != b["high_water"]:
                self._store.set(b["bracket_id"], high_water=high)  # type: ignore[attr-defined]
            return
        try:
            sent = self._om.modify_trigger(b["stop_order_id"], move.new_stop)  # type: ignore[attr-defined]
            new_stop = float(sent.trigger_price)  # on the instrument's tick (#75)
        except Exception as exc:  # stop stays where it was; next tick retries
            logger.warning("trail_modify_failed bracket=%s error=%s", b["bracket_id"], exc)
            self._store.set(b["bracket_id"], high_water=high)  # type: ignore[attr-defined]
            return
        self._store.advance(  # type: ignore[attr-defined]
            b,
            "stop_raised",
            rule=move.rule,
            inputs={
                "from": current,
                "to": new_stop,
                "high": high,
                "ltp": ltp,
                "atr": b["atr"],
            },
            trail_stop=new_stop,
            high_water=high,
            stop_moves=b["stop_moves"] + 1,
        )
        logger.info(
            "stop_raised bracket=%s %s %.2f -> %.2f",
            b["bracket_id"],
            move.rule,
            current,
            new_stop,
        )
