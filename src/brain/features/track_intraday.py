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

Live-only (injected by intraday task runner at 9:15–9:25 AM IST)
-----------------------------------------------------------------
  premarket_gap         → premarket_gap_pct
  opening_range         → orb_range_vs_20d_avg_ratio, orb_high
  nifty_direction       → nifty_intraday_direction
  bid_ask_quality       → bid_ask_spread_pct
  news_recency          → minutes_since_latest_news
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
        writes=("price_close", "avg_traded_value_20d", "avg_daily_volume_20d",
                "atr_14d", "volume_zscore_5d", "dma_20", "high_20d"),
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

    # ── Live-only: injected by intraday task runner at signal time ────────────
    # fn=None means the runner skips these; they are listed here so you can
    # see at a glance every feature key the signal generator may consume.

    FeatureComputer(
        fn=None,
        writes=("premarket_gap_pct",),
        essential=True,
        requires_live=True,
        # Source: (today_open - yesterday_close) / yesterday_close
        #         read from broker live quote at 9:15 AM IST
    ),
    FeatureComputer(
        fn=None,
        writes=("orb_range_vs_20d_avg_ratio", "orb_high"),
        essential=False,
        requires_live=True,
        # Source: first-15-min high/low range from minute-bar parquet lake
    ),
    FeatureComputer(
        fn=None,
        writes=("nifty_intraday_direction",),
        essential=False,
        requires_live=True,
        # Source: +1/0/-1 based on Nifty live price vs previous close
    ),
    FeatureComputer(
        fn=None,
        writes=("bid_ask_spread_pct",),
        essential=False,
        requires_live=True,
        # Source: broker live orderbook snapshot
    ),
    FeatureComputer(
        fn=None,
        writes=("minutes_since_latest_news",),
        essential=False,
        requires_live=True,
        # Source: (now - MAX(filings.observed_at)) / 60  at signal call time
    ),
]
