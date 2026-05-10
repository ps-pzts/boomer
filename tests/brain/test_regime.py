"""Tests for brain.regime — exhaustive taxonomy and stickiness.

Worked examples:
  bull_calm:     Nifty +3% above DMA, VIX 40th pct, breadth 65%  → bull_calm
  bull_volatile: Nifty +3% above DMA, VIX 60th pct, breadth 65%  → bull_volatile  (Q3-1 gap case)
  bull_volatile: Nifty +3% above DMA, VIX 85th pct, breadth 65%  → bear (VIX > 80th)
  sideways:      Nifty +2% above DMA, VIX 40th pct, breadth 55%  → sideways
  bear:          Nifty -1% below DMA, VIX 40th pct, breadth 55%  → bear
  bear:          Nifty +3% above DMA, VIX 40th pct, breadth 25%  → bear (breadth < 30%)
"""

import pytest

from brain.regime import INTRADAY_DOWNGRADE_PCT, RegimeDetector, _classify_raw
from capital.models import Regime


@pytest.fixture
def detector():
    return RegimeDetector()


class TestRawClassification:
    def test_bull_calm(self):
        r = _classify_raw(nifty_vs_200dma_pct=3.0, vix_percentile=40.0, breadth_pct=65.0)
        assert r == Regime.BULL_CALM

    def test_bull_volatile_vix_gap(self):
        # Q3-1 resolved: VIX 60th percentile + Nifty above DMA → bull_volatile (not unclassified)
        r = _classify_raw(nifty_vs_200dma_pct=3.0, vix_percentile=60.0, breadth_pct=65.0)
        assert r == Regime.BULL_VOLATILE

    def test_bull_volatile_breadth_mid(self):
        r = _classify_raw(nifty_vs_200dma_pct=6.0, vix_percentile=40.0, breadth_pct=50.0)
        assert r == Regime.BULL_VOLATILE

    def test_sideways(self):
        r = _classify_raw(nifty_vs_200dma_pct=2.0, vix_percentile=40.0, breadth_pct=55.0)
        assert r == Regime.SIDEWAYS

    def test_sideways_negative_but_within_5pct(self):
        r = _classify_raw(nifty_vs_200dma_pct=-3.0, vix_percentile=30.0, breadth_pct=55.0)
        assert r == Regime.SIDEWAYS

    def test_bear_below_dma(self):
        r = _classify_raw(nifty_vs_200dma_pct=-1.0, vix_percentile=40.0, breadth_pct=55.0)
        assert r == Regime.BEAR

    def test_bear_low_breadth(self):
        r = _classify_raw(nifty_vs_200dma_pct=3.0, vix_percentile=40.0, breadth_pct=25.0)
        assert r == Regime.BEAR

    def test_bear_high_vix(self):
        r = _classify_raw(nifty_vs_200dma_pct=3.0, vix_percentile=85.0, breadth_pct=65.0)
        assert r == Regime.BEAR

    def test_taxonomy_is_exhaustive_no_unclassified(self):
        # Sample grid — every combination must produce one of the 4 regimes
        for nifty in [-10, -3, 0, 3, 10]:
            for vix in [10, 40, 60, 75, 85]:
                for breadth in [20, 45, 65]:
                    r = _classify_raw(nifty, vix, breadth)
                    valid = (Regime.BULL_CALM, Regime.BULL_VOLATILE, Regime.SIDEWAYS, Regime.BEAR)
                    assert r in valid


class TestStickiness:
    def test_no_flip_without_consecutive_disagreement(self, detector):
        # Current is bull_calm; new raw is bull_volatile for only 2 days (< 3)
        history = [Regime.BULL_CALM, Regime.BULL_CALM, Regime.BULL_VOLATILE, Regime.BULL_VOLATILE]
        result = detector.detect(6.0, 60.0, 65.0, history)
        assert result == Regime.BULL_CALM

    def test_flips_after_3_consecutive_days(self, detector):
        history = [
            Regime.BULL_CALM,
            Regime.BULL_VOLATILE,
            Regime.BULL_VOLATILE,
            Regime.BULL_VOLATILE,
        ]
        result = detector.detect(6.0, 60.0, 65.0, history)
        assert result == Regime.BULL_VOLATILE

    def test_short_history_uses_raw_regime(self, detector):
        # Fewer than 3 days of history → use raw immediately
        result = detector.detect(6.0, 60.0, 65.0, recent_regimes=[Regime.BULL_CALM])
        assert result == Regime.BULL_VOLATILE

    def test_empty_history_uses_raw_regime(self, detector):
        result = detector.detect(6.0, 60.0, 65.0, recent_regimes=[])
        assert result == Regime.BULL_VOLATILE


class TestIntradayDowngrade:
    def test_no_downgrade_above_threshold(self, detector):
        result = detector.intraday_downgrade(Regime.BULL_CALM, nifty_intraday_change_pct=-1.0)
        assert result == Regime.BULL_CALM

    def test_downgrade_at_threshold(self, detector):
        # -1.5% exactly triggers downgrade
        result = detector.intraday_downgrade(
            Regime.BULL_CALM, nifty_intraday_change_pct=INTRADAY_DOWNGRADE_PCT
        )
        assert result == Regime.BEAR

    def test_downgrade_below_threshold(self, detector):
        result = detector.intraday_downgrade(Regime.BULL_VOLATILE, nifty_intraday_change_pct=-2.5)
        assert result == Regime.BEAR

    def test_positive_intraday_no_downgrade(self, detector):
        result = detector.intraday_downgrade(Regime.BULL_CALM, nifty_intraday_change_pct=1.0)
        assert result == Regime.BULL_CALM
