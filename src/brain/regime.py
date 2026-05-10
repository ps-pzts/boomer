from __future__ import annotations

from capital.models import Regime

# Minimum consecutive days of disagreement before the regime flips (stickiness).
REGIME_STICKINESS_DAYS = 3

# Intraday Nifty drop threshold that downgrades the intraday track to bear for the session.
INTRADAY_DOWNGRADE_PCT = -1.5


def _classify_raw(
    nifty_vs_200dma_pct: float,
    vix_percentile: float,
    breadth_pct: float,
) -> Regime:
    """Map market inputs to a regime.

    Q3-1 (resolved 2026-05-10): taxonomy is now exhaustive.
    Precedence: bear > sideways > bull_volatile > bull_calm.

    Args:
        nifty_vs_200dma_pct: % distance of Nifty from its 200 DMA;
            positive = above, negative = below.
        vix_percentile: India VIX position within its trailing 252-day range (0–100).
        breadth_pct: % of Nifty 500 stocks trading above their 50 DMA (0–100).
    """
    # Hard bear triggers: very low breadth, extreme VIX, or below-DMA + elevated fear
    # Below DMA + VIX >= 35th pct = downtrend forming (not mere consolidation)
    if breadth_pct < 30.0 or vix_percentile > 80.0:
        return Regime.BEAR
    if nifty_vs_200dma_pct < 0 and vix_percentile >= 35.0:
        return Regime.BEAR

    # bull_calm: low VIX (< 50th pct) + broad breadth (>= 60%) — healthy uptrend
    if vix_percentile < 50.0 and breadth_pct >= 60.0:
        return Regime.BULL_CALM

    # bull_volatile: far above DMA (> 5%) OR elevated VIX
    # Covers the Q3-1 "Volatile Uptrend" gap (VIX 50-80th pct + Nifty above DMA).
    if nifty_vs_200dma_pct > 5.0 or vix_percentile >= 50.0:
        return Regime.BULL_VOLATILE

    # Sideways: near DMA, moderate breadth, low-to-moderate VIX, low fear below DMA
    return Regime.SIDEWAYS


class RegimeDetector:
    """Stage 1 — detects the current market regime with stickiness."""

    def detect(
        self,
        nifty_vs_200dma_pct: float,
        vix_percentile: float,
        breadth_pct: float,
        recent_regimes: list[Regime],
    ) -> Regime:
        """Compute the regime for today, applying stickiness.

        Args:
            nifty_vs_200dma_pct: % distance of Nifty from 200 DMA.
            vix_percentile: VIX percentile within 252-day range (0–100).
            breadth_pct: % of Nifty 500 above 50 DMA (0–100).
            recent_regimes: ordered list of previous regimes (most recent last).
                Must contain at least REGIME_STICKINESS_DAYS entries for stickiness
                to take effect; if shorter, no stickiness is applied.
        """
        new_raw = _classify_raw(nifty_vs_200dma_pct, vix_percentile, breadth_pct)
        return _apply_stickiness(new_raw, recent_regimes)

    def detect_no_stickiness(
        self,
        nifty_vs_200dma_pct: float,
        vix_percentile: float,
        breadth_pct: float,
    ) -> Regime:
        """Raw classification without stickiness — useful for unit testing."""
        return _classify_raw(nifty_vs_200dma_pct, vix_percentile, breadth_pct)

    def intraday_downgrade(
        self,
        morning_regime: Regime,
        nifty_intraday_change_pct: float,
    ) -> Regime:
        """Apply the intraday -1.5% downgrade rule.

        If Nifty drops more than 1.5% from the previous close during market hours,
        the effective intraday regime becomes bear for the remainder of the session.
        Long-term and swing morning-batch decisions are NOT affected.
        """
        if nifty_intraday_change_pct <= INTRADAY_DOWNGRADE_PCT:
            return Regime.BEAR
        return morning_regime


def _apply_stickiness(new_regime: Regime, recent_regimes: list[Regime]) -> Regime:
    """Require REGIME_STICKINESS_DAYS trailing entries matching new_regime before flipping.

    recent_regimes are raw (pre-stickiness) classifications. If the last N entries
    all match new_regime, it's established enough to flip. Otherwise, return the most
    recent non-new_regime entry as the established regime.
    If recent history is insufficient, return the new raw regime immediately.
    """
    if len(recent_regimes) < REGIME_STICKINESS_DAYS:
        return new_regime

    # Count consecutive trailing entries that match new_regime
    streak = 0
    for r in reversed(recent_regimes):
        if r == new_regime:
            streak += 1
        else:
            break

    if streak >= REGIME_STICKINESS_DAYS:
        return new_regime

    # Streak too short — find the established regime (most recent non-new entry)
    for r in reversed(recent_regimes):
        if r != new_regime:
            return r
    return new_regime
