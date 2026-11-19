-- B7 PR 1 — paper/live switch (designs/intraday-live-pipeline.md §5).
-- Live order placement needs BOTH BOOMER_TRADING_MODE=live AND the latest row
-- here having enabled=1. No rows means disabled: the system trades on paper.
-- Append-only: every change is a new row, so who enabled live trading and why
-- is always auditable. A separate table (not a risk_config column) because
-- risk_config is versioned by copying rows, which would silently reset a flag.

CREATE TABLE live_trading_authorisation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    actor       TEXT    NOT NULL,                -- who made the change
    reason      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL                 -- IST ISO timestamp
);
