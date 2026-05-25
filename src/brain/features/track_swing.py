"""Swing track feature registry.

Open this file to see every feature the swing signal generator needs and
which computer produces it.

To add a new strategy
---------------------
1. Write a compute_* function in computers.py or computers_market.py.
2. Append a FeatureComputer entry to SWING_COMPUTERS below.
   If the new computer reads features written by a prior one (e.g.
   compute_technical_pattern_score reads dma_20), place it AFTER that entry.

To disable a strategy
---------------------
Comment out its FeatureComputer entry.

Batch computers — all run nightly by tasks_brain
-------------------------------------------------
  price_features            → price_close, avg_traded_value_20d, avg_daily_volume_20d,
                               atr_14d, volume_zscore_5d, dma_20, dma_50, dma_200, high_20d
  technical_pattern_score   → technical_pattern_score   [reads dma_20, dma_50, price_close, atr_14d]
  filing_count              → filing_count_7d
  catalyst_proximity        → days_to_next_catalyst
  sector_rs                 → sector_relative_strength_20d
  price_mode                → price_mode_classifier
  filing_sentiment          → filing_bullish_count_90d, filing_bearish_count_90d,
                               has_auditor_change_90d, has_pledging_increase_90d
  smart_money               → smart_money_net_buy_value_90d, smart_money_buyer_count_90d
  promoter                  → promoter_holding_pct_change_90d, promoter_open_market_buy_count_90d,
                               promoter_pledge_pct_current
  earnings_quality          → revenue_growth_yoy_pct, opm_trend_4q, cfo_pat_ratio_latest
"""

from brain.features.computers import (
    compute_catalyst_proximity_features,
    compute_earnings_quality_features,
    compute_filing_count_features,
    compute_filing_sentiment_features,
    compute_price_features,
    compute_promoter_features,
    compute_smart_money_features,
)
from brain.features.computers_market import (
    compute_price_mode_classifier,
    compute_sector_relative_strength,
    compute_technical_pattern_score,
)
from brain.features.runner import FeatureComputer

SWING_COMPUTERS: list[FeatureComputer] = [

    # price_features must be first — several computers below read its output
    FeatureComputer(
        fn=compute_price_features,
        writes=("price_close", "avg_traded_value_20d", "avg_daily_volume_20d",
                "atr_14d", "volume_zscore_5d", "dma_20", "dma_50", "dma_200", "high_20d"),
        essential=True,
    ),
    # Reads dma_20, dma_50, price_close, atr_14d — must follow price_features
    FeatureComputer(
        fn=compute_technical_pattern_score,
        writes=("technical_pattern_score",),
        essential=False,  # non-essential: graceful if DMA data absent in live mode
    ),
    FeatureComputer(
        fn=compute_filing_count_features,
        writes=("filing_count_7d",),
        essential=False,
    ),
    FeatureComputer(
        fn=compute_catalyst_proximity_features,
        writes=("days_to_next_catalyst",),
        essential=False,
    ),
    FeatureComputer(
        fn=compute_sector_relative_strength,
        writes=("sector_relative_strength_20d",),
        essential=False,  # requires sector_classifications to be populated
    ),
    FeatureComputer(
        fn=compute_price_mode_classifier,
        writes=("price_mode_classifier",),
        essential=False,
    ),
    FeatureComputer(
        fn=compute_filing_sentiment_features,
        writes=("filing_bullish_count_90d", "filing_bearish_count_90d",
                "has_auditor_change_90d", "has_pledging_increase_90d"),
        essential=False,
    ),
    FeatureComputer(
        fn=compute_smart_money_features,
        writes=("smart_money_net_buy_value_90d", "smart_money_buyer_count_90d"),
        essential=False,
    ),
    FeatureComputer(
        fn=compute_promoter_features,
        writes=("promoter_holding_pct_change_90d", "promoter_open_market_buy_count_90d",
                "promoter_pledge_pct_current"),
        essential=False,
    ),
    FeatureComputer(
        fn=compute_earnings_quality_features,
        writes=("revenue_growth_yoy_pct", "opm_trend_4q", "cfo_pat_ratio_latest"),
        essential=False,
    ),
]
