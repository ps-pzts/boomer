-- Remove swing and long_term tracks — intraday is now the only track.
-- Historical rows in signals/recommendations/positions with track='swing'
-- or track='long_term' are left as-is (audit trail); those columns have
-- no CHECK constraint, so no data migration is needed for them.

ALTER TABLE capital_ledger DROP COLUMN long_term_allocated_pct;
ALTER TABLE capital_ledger DROP COLUMN swing_allocated_pct;
ALTER TABLE capital_ledger DROP COLUMN long_term_deployed;
ALTER TABLE capital_ledger DROP COLUMN swing_deployed;

ALTER TABLE risk_config DROP COLUMN risk_per_swing_trade_pct;
ALTER TABLE risk_config DROP COLUMN risk_per_long_term_trade_pct;
ALTER TABLE risk_config DROP COLUMN swing_weekly_loss_limit_pct;
ALTER TABLE risk_config DROP COLUMN swing_30d_loss_count;
ALTER TABLE risk_config DROP COLUMN live_backtest_ratio_long_term;
ALTER TABLE risk_config DROP COLUMN live_backtest_ratio_swing;

-- The human-approval feature (dashboard /approvals, Telegram approve/reject)
-- existed only to gate long_term recommendations. With long_term gone,
-- APM auto-decides every recommendation — this column is never read or
-- written again.
ALTER TABLE recommendations DROP COLUMN requires_human;
