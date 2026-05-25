"""Long-term track feature registry.

Open this file to see every feature the long-term signal generator needs and
which computer produces it.

To add a new strategy
---------------------
1. Write a compute_* function in computers.py or computers_market.py.
2. Append a FeatureComputer entry to LONG_TERM_COMPUTERS below.

To disable a strategy
---------------------
Comment out its FeatureComputer entry.

All long-term computers run nightly (low urgency — results catalysts are
captured by the filing sentiment pipeline with a ~2-hour lag from announcement).

Batch computers — run nightly by tasks_brain
---------------------------------------------
  price_features      → price_close, avg_traded_value_20d, avg_daily_volume_20d,
                         atr_14d, volume_zscore_5d, dma_20, dma_50, dma_200, high_20d
  filing_sentiment    → filing_bullish_count_90d, filing_bearish_count_90d,
                         has_auditor_change_90d, has_pledging_increase_90d
  smart_money         → smart_money_net_buy_value_90d, smart_money_buyer_count_90d
  promoter            → promoter_holding_pct_change_90d, promoter_open_market_buy_count_90d,
                         promoter_pledge_pct_current
  earnings_quality    → revenue_growth_yoy_pct, opm_trend_4q, cfo_pat_ratio_latest

Note: pe_percentile_5y is not yet computable from the current schema (requires
5-year PE history).  The valuation sub-signal is skipped until a PE history
table is added.  Track this in open-questions.md (Q3-4).
"""

from brain.features.computers import (
    compute_earnings_quality_features,
    compute_filing_sentiment_features,
    compute_price_features,
    compute_promoter_features,
    compute_smart_money_features,
)
from brain.features.runner import FeatureComputer

LONG_TERM_COMPUTERS: list[FeatureComputer] = [

    FeatureComputer(
        fn=compute_price_features,
        writes=("price_close", "avg_traded_value_20d", "avg_daily_volume_20d",
                "atr_14d", "volume_zscore_5d", "dma_20", "dma_50", "dma_200", "high_20d"),
        essential=True,
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
