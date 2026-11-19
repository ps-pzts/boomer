-- B7 PR 3 — daily intraday watchlist (designs/intraday-live-pipeline.md §3.6, Q3).
-- One row per watched stock per session. Written at 09:10 IST by the
-- intraday_watchlist task; re-running the task the same day replaces the rows.
-- Read back at boot to re-subscribe the feed after a mid-session restart.

CREATE TABLE intraday_watchlist (
    session_date          TEXT    NOT NULL,              -- ISO date (IST)
    stock_symbol          TEXT    NOT NULL,
    exchange              TEXT    NOT NULL DEFAULT 'NSE',
    rank                  INTEGER NOT NULL CHECK (rank >= 1),
    selection_reason      TEXT    NOT NULL
        CHECK (selection_reason IN ('batch_score', 'liquidity_fill')),
    batch_raw_score       REAL,                          -- generator score on batch features
    batch_direction       TEXT,
    avg_traded_value_20d  REAL    NOT NULL,              -- ₹, from the price features
    created_at            TEXT    NOT NULL,              -- IST ISO timestamp
    PRIMARY KEY (session_date, stock_symbol, exchange)
);

CREATE INDEX idx_intraday_watchlist_rank ON intraday_watchlist(session_date, rank);
