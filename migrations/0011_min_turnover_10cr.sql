-- #38 — single liquidity floor: ₹10 crore average daily turnover (operator decision
-- 2026-11-19). The signal generator's hardcoded ₹10 cr gate is removed in the same
-- change; risk_config is now the only place the floor lives.
--
-- risk_config is versioned: add a new version copying the latest one with the new
-- floor. Only applies to databases that already have a config and a lower floor; a
-- fresh database is seeded with 10.0 by RiskConfigStore.seed_defaults().
-- Explicit column list on purpose: a positional INSERT ... SELECT silently wrote a
-- timestamp into a numeric column once already (#25).

INSERT INTO risk_config (
    config_id, version, effective_from,
    risk_per_intraday_trade_pct, intraday_daily_loss_limit_pct,
    portfolio_daily_loss_limit_pct, portfolio_weekly_loss_limit_pct,
    portfolio_max_drawdown_pct, single_stock_cap_pct, sector_cap_pct,
    correlation_cluster_cap_pct, intraday_consecutive_loss_count,
    nifty_intraday_pause_pct, live_backtest_ratio_intraday,
    sentiment_confidence_threshold, created_at,
    min_stock_price, min_avg_daily_volume, min_avg_daily_turnover_cr
)
SELECT
    lower(hex(randomblob(16))), version + 1, date('now', '+330 minutes'),
    risk_per_intraday_trade_pct, intraday_daily_loss_limit_pct,
    portfolio_daily_loss_limit_pct, portfolio_weekly_loss_limit_pct,
    portfolio_max_drawdown_pct, single_stock_cap_pct, sector_cap_pct,
    correlation_cluster_cap_pct, intraday_consecutive_loss_count,
    nifty_intraday_pause_pct, live_backtest_ratio_intraday,
    sentiment_confidence_threshold, strftime('%Y-%m-%dT%H:%M:%f', 'now', '+330 minutes'),
    min_stock_price, min_avg_daily_volume, 10.0
FROM risk_config
WHERE version = (SELECT MAX(version) FROM risk_config)
  AND min_avg_daily_turnover_cr < 10.0;
