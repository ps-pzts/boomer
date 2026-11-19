-- Trailing stop (#71): breakeven at +1R, then 1 x ATR below the high since entry.
-- stop_price keeps the planned (initial) stop for audit; trail_stop is the stop's
-- current trigger once it has been raised. Stops only ever move up (CLAUDE.md §10).

ALTER TABLE intraday_brackets ADD COLUMN trail_stop REAL;              -- NULL = never raised
ALTER TABLE intraday_brackets ADD COLUMN high_water REAL;              -- highest LTP seen since entry
ALTER TABLE intraday_brackets ADD COLUMN stop_moves INTEGER NOT NULL DEFAULT 0;
