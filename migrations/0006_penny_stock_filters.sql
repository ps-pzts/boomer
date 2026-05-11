-- Phase 3 — penny stock / micro-cap quality filters in risk_config.
-- Three thresholds added; defaults match the spec from the design review:
--   min_stock_price          ₹100   (price gate)
--   min_avg_daily_volume     500000 shares/day 20d avg (liquidity gate)
--   min_avg_daily_turnover_cr  5.0  ₹ crore/day 20d avg (market-cap proxy gate;
--                                   true market cap requires shares_outstanding data)

ALTER TABLE risk_config ADD COLUMN min_stock_price              REAL    NOT NULL DEFAULT 100;
ALTER TABLE risk_config ADD COLUMN min_avg_daily_volume         INTEGER NOT NULL DEFAULT 500000;
ALTER TABLE risk_config ADD COLUMN min_avg_daily_turnover_cr    REAL    NOT NULL DEFAULT 5.0;
