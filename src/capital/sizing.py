"""Intraday position sizing (#69).

Operator decision 2026-11-19: risk-based size, **shrunk** to the single-stock cap
instead of rejected by it. With the defaults (0.5% risk, 1.5×ATR stop, 5% cap)
a full-risk position is worth 15-20% of capital, so the cap almost always binds
and the real risk per trade is smaller than 0.5%. That is deliberate for paper.

Worked example (₹10,00,000 capital, all intraday; bull_calm scale 1.0):
  entry 1,000.00, stop 970.00 -> risk/share 30.00
  risk-based: 10,00,000 × 0.5% = 5,000 / 30 = 166 shares (₹1,66,000)
  cap:        10,00,000 × 5%   = 50,000 / 1,000 = 50 shares
  quantity = 50 (cap binds), risk = 50 × 30 = ₹1,500
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class Sizing:
    quantity: int
    risk_shares: int  # before the cap (after the regime scale)
    cap_shares: int
    binding: str  # "risk" | "single_stock_cap"
    risk_rupees: Decimal  # quantity × (entry - stop)


def _floor(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_DOWN))


def intraday_quantity(
    *,
    bucket_capital: Decimal,
    total_capital: Decimal,
    risk_pct: Decimal,
    single_stock_cap_pct: Decimal,
    regime_scale: Decimal,
    entry: Decimal,
    stop: Decimal,
) -> Sizing:
    """Shares to buy. quantity 0 means the trade is too small to take."""
    if entry <= 0 or stop <= 0 or stop >= entry:
        raise ValueError(f"need 0 < stop < entry, got stop={stop} entry={entry}")
    per_share = entry - stop
    risk_shares = _floor(_floor(bucket_capital * risk_pct / per_share) * regime_scale)
    cap_shares = _floor(total_capital * single_stock_cap_pct / entry)
    quantity = max(0, min(risk_shares, cap_shares))
    return Sizing(
        quantity=quantity,
        risk_shares=risk_shares,
        cap_shares=cap_shares,
        binding="single_stock_cap" if cap_shares < risk_shares else "risk",
        risk_rupees=per_share * quantity,
    )
