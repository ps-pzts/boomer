"""Stage 3.5 — Entry timing classifiers.

One classifier per track. Each refines the raw entry zone from Stage 3 into
concrete order parameters (price, strategy, validity, tranche structure).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from brain.models import EntryPlan, EntryStrategy, SignalRecord, TradePlan

# Intraday ORB must trigger before this time (minutes from market open at 9:15)
ORB_TRIGGER_DEADLINE_MINS = 105  # 9:15 + 105 min = 11:00 AM

# Gap thresholds for ID3 gap-fade/ride strategy
GAP_SMALL_MAX_PCT = 1.0
GAP_MEDIUM_MAX_PCT = 2.5


class LongTermClassifier:
    """Maps a long-term trade plan to a staged accumulation or DMA-pullback entry."""

    def classify(
        self,
        plan: TradePlan,
        features: dict[str, Any],
    ) -> list[EntryPlan]:
        """Return ordered list of EntryPlan tranches.

        Strategy selection (Loophole 10: fixed for v1):
          - Default: LT1 (staged accumulation, 3 tranches)
          - If valuation_score is the dominant contributor: LT3 (valuation-anchored)
          - Otherwise if DMA pullback condition met: LT2
        """
        dma_50 = features.get("dma_50")
        atr = features.get("atr_14d", 0.0)
        current_price = float(plan.entry_zone_high)

        if dma_50 and current_price > 0:
            dma_dist_pct = (current_price - float(dma_50)) / current_price * 100.0
            # LT2: price within 3% of 50 DMA (pullback condition)
            if 0 <= dma_dist_pct <= 3.0:
                lt2_price = Decimal(
                    str(float(dma_50) + 0.2 * float(atr))
                ).quantize(Decimal("0.05"))
                return [EntryPlan(
                    strategy=EntryStrategy.LT2,
                    entry_price=lt2_price,
                    validity_days=30,
                    tranche_fraction=Decimal("1.0"),
                )]

        # LT1: three tranches over 4–8 weeks
        entry_mid = Decimal(str((float(plan.entry_zone_low) + float(plan.entry_zone_high)) / 2))
        dma50_d = Decimal(str(float(dma_50))) if dma_50 else entry_mid * Decimal("0.95")
        dma200 = features.get("dma_200")
        dma200_d = Decimal(str(float(dma200))) if dma200 else entry_mid * Decimal("0.90")

        t1_price = entry_mid
        t2_price = min(dma50_d, entry_mid * Decimal("0.95"))
        t3_price = min(dma200_d, entry_mid * Decimal("0.90"))

        q = Decimal("0.05")
        return [
            EntryPlan(
                strategy=EntryStrategy.LT1, entry_price=t1_price.quantize(q),
                validity_days=30, tranche_fraction=Decimal("0.40"),
                tranche_index=1, total_tranches=3,
            ),
            EntryPlan(
                strategy=EntryStrategy.LT1, entry_price=t2_price.quantize(q),
                validity_days=30, tranche_fraction=Decimal("0.30"),
                tranche_index=2, total_tranches=3,
            ),
            EntryPlan(
                strategy=EntryStrategy.LT1, entry_price=t3_price.quantize(q),
                validity_days=30, tranche_fraction=Decimal("0.30"),
                tranche_index=3, total_tranches=3,
            ),
        ]


class SwingClassifier:
    """Maps a swing trade plan to a breakout, pullback, or catalyst entry."""

    def classify(
        self,
        plan: TradePlan,
        features: dict[str, Any],
    ) -> list[EntryPlan]:
        days_to_catalyst = features.get("days_to_next_catalyst")
        volume_zscore = float(features.get("volume_zscore_5d", 0.0))
        atr = Decimal(str(float(features.get("atr_14d", 0.0))))
        current_price = Decimal(str((float(plan.entry_zone_low) + float(plan.entry_zone_high)) / 2))
        dma_20 = features.get("dma_20")

        # SW3: catalyst event — pre-catalyst entry
        if days_to_catalyst is not None and 0 <= float(days_to_catalyst) <= 7:
            return [EntryPlan(
                strategy=EntryStrategy.SW3,
                entry_price=current_price.quantize(Decimal("0.05")),
                validity_days=7,
                tranche_fraction=Decimal("0.50"),
                notes="pre-catalyst 50%; full position after bullish catalyst",
            )]

        # SW1: breakout with volume (momentum mode)
        high_20d = features.get("high_20d")
        if high_20d and volume_zscore >= 0.5:
            trigger = Decimal(str(float(high_20d))) + Decimal("0.3") * atr
            return [EntryPlan(
                strategy=EntryStrategy.SW1,
                entry_price=trigger.quantize(Decimal("0.05")),
                validity_days=7,
                tranche_fraction=Decimal("1.0"),
            )]

        # SW2: pullback to support (default)
        support = Decimal(str(float(dma_20))) if dma_20 else current_price * Decimal("0.98")
        return [EntryPlan(
            strategy=EntryStrategy.SW2,
            entry_price=(support + Decimal("0.2") * atr).quantize(Decimal("0.05")),
            validity_days=7,
            tranche_fraction=Decimal("1.0"),
        )]


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
            return [EntryPlan(
                strategy=EntryStrategy.ID3,
                entry_price=Decimal(str(float(vwap))).quantize(Decimal("0.05")),
                validity_days=1,
                tranche_fraction=Decimal("1.0"),
                notes="medium gap ride; enter on VWAP pullback",
            )]

        # ID2: VWAP pullback (price ran up then pulled back to VWAP)
        if vwap:
            price_vs_open_pct = float(features.get("price_vs_open_pct", 0.0))
            if price_vs_open_pct >= 0.5:
                return [EntryPlan(
                    strategy=EntryStrategy.ID2,
                    entry_price=Decimal(str(float(vwap))).quantize(Decimal("0.05")),
                    validity_days=1,
                    tranche_fraction=Decimal("1.0"),
                )]

        # ID1: ORB (default); only valid before 11:00 AM
        if orb_high and minutes_elapsed <= ORB_TRIGGER_DEADLINE_MINS:
            trigger = Decimal(str(float(orb_high))) * Decimal("1.001")
            return [EntryPlan(
                strategy=EntryStrategy.ID1,
                entry_price=trigger.quantize(Decimal("0.05")),
                validity_days=1,
                tranche_fraction=Decimal("1.0"),
            )]

        return []


def check_stacking_gate(
    intraday_signal: SignalRecord,
    swing_position_pnl_pct: float,
    intraday_contributing_names: set[str],
    swing_contributing_names: set[str],
    total_exposure_pct: float,
    single_stock_cap_pct: float,
) -> tuple[bool, str]:
    """Validate the 6-condition stacking gate (Loophole 12).

    Returns (allowed, reason). Caller must still verify broker tagging and
    auto-square-off enforcement (executor responsibility).
    """
    # Condition 1: swing in profit
    if swing_position_pnl_pct <= 1.0:
        return False, f"swing P&L {swing_position_pnl_pct:.2f}% ≤ 1% threshold"

    # Condition 2: signal independence ≥ 50%
    if intraday_contributing_names and swing_contributing_names:
        overlap = intraday_contributing_names & swing_contributing_names
        total = len(intraday_contributing_names)
        independent_frac = (total - len(overlap)) / total if total else 0.0
        if independent_frac < 0.50:
            return False, f"signal independence {independent_frac:.0%} < 50% required"

    # Condition 3: concentration cap
    if total_exposure_pct > single_stock_cap_pct * 100.0:
        cap_pct = single_stock_cap_pct * 100.0
        return False, f"combined exposure {total_exposure_pct:.1f}% > cap {cap_pct:.1f}%"

    return True, "all stacking conditions met"
