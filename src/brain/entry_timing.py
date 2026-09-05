"""Stage 3.5 — Entry timing classifier.

Refines the raw entry zone from Stage 3 into concrete order parameters
(price, strategy, validity, tranche structure) for the intraday track.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from brain.models import EntryPlan, EntryStrategy, TradePlan

# Intraday ORB must trigger before this time (minutes from market open at 9:15)
ORB_TRIGGER_DEADLINE_MINS = 105  # 9:15 + 105 min = 11:00 AM

# Gap thresholds for ID3 gap-fade/ride strategy
GAP_SMALL_MAX_PCT = 1.0
GAP_MEDIUM_MAX_PCT = 2.5


class IntradayClassifier:
    """Maps an intraday trade plan to an ORB, VWAP pullback, or gap strategy."""

    def classify(
        self,
        plan: TradePlan,
        features: dict[str, Any],
    ) -> list[EntryPlan]:
        gap_pct = float(features.get("premarket_gap_pct", 0.0))
        vwap = features.get("vwap_current")
        orb_high = features.get("orb_high")
        minutes_elapsed = float(features.get("minutes_since_market_open", 0.0))

        # ID3: gap strategy (large gap → skip handled by signal layer; medium/small here)
        if abs(gap_pct) > GAP_MEDIUM_MAX_PCT:
            # Large gap: unreliable — no entry plan (caller sees empty list = skip)
            return []

        if abs(gap_pct) >= GAP_SMALL_MAX_PCT and vwap:
            # Medium gap with news: ride on VWAP pullback
            return [
                EntryPlan(
                    strategy=EntryStrategy.ID3,
                    entry_price=Decimal(str(float(vwap))).quantize(Decimal("0.05")),
                    validity_days=1,
                    tranche_fraction=Decimal("1.0"),
                    notes="medium gap ride; enter on VWAP pullback",
                )
            ]

        # ID2: VWAP pullback (price ran up then pulled back to VWAP)
        if vwap:
            price_vs_open_pct = float(features.get("price_vs_open_pct", 0.0))
            if price_vs_open_pct >= 0.5:
                return [
                    EntryPlan(
                        strategy=EntryStrategy.ID2,
                        entry_price=Decimal(str(float(vwap))).quantize(Decimal("0.05")),
                        validity_days=1,
                        tranche_fraction=Decimal("1.0"),
                    )
                ]

        # ID1: ORB (default); only valid before 11:00 AM
        if orb_high and minutes_elapsed <= ORB_TRIGGER_DEADLINE_MINS:
            trigger = Decimal(str(float(orb_high))) * Decimal("1.001")
            return [
                EntryPlan(
                    strategy=EntryStrategy.ID1,
                    entry_price=trigger.quantize(Decimal("0.05")),
                    validity_days=1,
                    tranche_fraction=Decimal("1.0"),
                )
            ]

        return []
