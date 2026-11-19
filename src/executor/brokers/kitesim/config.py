"""KiteSim settings and the deterministic intrabar path used by the backtester.

Every threshold is a rate or a percentage (CLAUDE.md §9: no rupee amounts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class KiteSimConfig:
    # MIS margin = order value x rate. Zerodha equity MIS is roughly 5x leverage.
    mis_margin_rate: float = 0.20
    # CNC (delivery) buys block the full value.
    cnc_margin_rate: float = 1.0
    # RMS margin check on orders that add exposure. The backtester keeps its own
    # cash ledger (as it did before #86) and turns this off.
    check_margin: bool = True
    # Price band around the day's reference price (first price seen, then the
    # previous close). None disables the circuit check.
    circuit_pct: float | None = 0.20
    # Largest quantity a single order may carry. None = no freeze limit.
    freeze_quantity: int | None = None
    # New MIS exposure is refused from this IST time on (Zerodha: 15:20).
    mis_cutoff: time = time(15, 20)
    # Most shares one order fills per price event. None = the whole order.
    liquidity_per_tick: int | None = None
    user_id: str = "PAPER1"
    seed: int = 0


def bar_path(o: float, h: float, lo: float, c: float) -> list[float]:
    """open -> nearer extreme -> farther extreme -> close.

    The extreme closer to the open is visited first; on a tie the low comes first
    (a long's stop before its target, as the pre-#86 simulator's stop-first OCO rule did)."""
    first, second = (lo, h) if abs(o - lo) <= abs(h - o) else (h, lo)
    return [o, first, second, c]
