"""Intraday track feature registry.

Open this file to see every feature the intraday signal generator needs,
where each feature comes from, and whether it is batch-computable or live-only.

To add a new strategy
---------------------
1. Write a compute_* function in computers.py or computers_market.py.
2. Append a FeatureComputer entry to INTRADAY_COMPUTERS below.

To disable a strategy
---------------------
Comment out its FeatureComputer entry.  The signal generator will receive
None for its feature keys and skip those sub-signals gracefully.

Batch computers (run nightly / pre-market by tasks_brain)
----------------------------------------------------------
  price_features        → price_close, avg_traded_value_20d, avg_daily_volume_20d,
                           atr_14d, volume_zscore_5d, dma_20, high_20d
  fo_features           → fo_oi_overnight_change_pct, fo_max_pain_proximity_pct
  beta_features         → beta_20d  (needs NIFTY 50 rows in prices table)
  overnight_news        → overnight_news_sentiment

Live-only (computed each cycle by brain.features.live.compute_live_features)
---------------------------------------------------------------------------
  premarket_gap         → premarket_gap_pct
  opening_range         → orb_range_vs_atr, orb_high, orb_low
  entry context         → vwap_current, ltp, price_vs_open_pct, minutes_since_market_open
  nifty_direction       → nifty_intraday_direction
  bid_ask_quality       → bid_ask_spread_pct
  news_recency          → minutes_since_latest_news

Live features are never written to the features table (design §3.4: it is
date-granular, so intraday values would leak to earlier same-day queries).
"""

from brain.features.computers import compute_price_features
from brain.features.computers_market import (
    compute_beta_features,
    compute_fo_features,
    compute_overnight_news_features,
)
from brain.features.runner import FeatureComputer

INTRADAY_COMPUTERS: list[FeatureComputer] = [
    # ── Batch-computable (run before market open) ─────────────────────────────
    FeatureComputer(
        fn=compute_price_features,
        writes=(
            "price_close",
            "avg_traded_value_20d",
            "avg_daily_volume_20d",
            "atr_14d",
            "volume_zscore_5d",
            "dma_20",
            "high_20d",
        ),
        essential=True,
    ),
    FeatureComputer(
        fn=compute_fo_features,
        writes=("fo_oi_overnight_change_pct", "fo_max_pain_proximity_pct"),
        essential=True,
    ),
    FeatureComputer(
        fn=compute_beta_features,
        writes=("beta_20d",),
        essential=False,  # skipped silently when NIFTY 50 prices absent
    ),
    FeatureComputer(
        fn=compute_overnight_news_features,
        writes=("overnight_news_sentiment",),
        essential=False,
    ),
    # ── Live-only: computed at signal time by brain.features.live ─────────────
    # fn=None means the batch runner skips these; they are listed here so you can
    # see at a glance every feature key the signal generator and classifier consume.
    FeatureComputer(
        fn=None,
        writes=("premarket_gap_pct",),
        essential=True,
        requires_live=True,
        # (day_open - prev_close) / prev_close x 100, from the 09:15 tick
    ),
    FeatureComputer(
        fn=None,
        writes=("orb_range_vs_atr", "orb_high", "orb_low"),
        essential=False,
        requires_live=True,
        # 09:15-09:29 minute bars (LiveBarStore); range / atr_14d (design Q4)
    ),
    FeatureComputer(
        fn=None,
        writes=("vwap_current", "ltp", "price_vs_open_pct", "minutes_since_market_open"),
        essential=False,
        requires_live=True,
        # latest tick (exchange VWAP = avg_traded_price) and the IST clock
    ),
    FeatureComputer(
        fn=None,
        writes=("nifty_intraday_direction",),
        essential=False,
        requires_live=True,
        # +1/0/-1: NIFTY 50 tick vs prev close with a 0.10% deadband
    ),
    FeatureComputer(
        fn=None,
        writes=("bid_ask_spread_pct",),
        essential=False,
        requires_live=True,
        # best bid/ask from FULL-mode depth
    ),
    FeatureComputer(
        fn=None,
        writes=("minutes_since_latest_news",),
        essential=False,
        requires_live=True,
        # now - MAX(filings.observed_at), observed_at read as IST (design L13)
    ),
]
