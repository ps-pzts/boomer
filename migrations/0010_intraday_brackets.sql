-- B7 PR 4b — one row per intraday trade (designs/intraday-live-pipeline.md §3.5).
-- The restart-safe source of truth for TradeSupervisor: which orders belong to
-- the trade and where it is in its lifecycle. Order fills themselves live in
-- orders/executions; P&L is computed from those fills at close.
--
-- state: entry_pending -> open -> exiting -> closed
--        entry_pending -> cancelled            (entry expired / rejected, nothing filled)

CREATE TABLE intraday_brackets (
    bracket_id          TEXT    PRIMARY KEY,
    session_date        TEXT    NOT NULL,                    -- ISO date (IST)
    symbol              TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    planned_quantity    INTEGER NOT NULL CHECK (planned_quantity > 0),
    filled_quantity     INTEGER NOT NULL DEFAULT 0,
    entry_price         REAL    NOT NULL,                    -- planned LIMIT price
    stop_price          REAL    NOT NULL,
    target_price        REAL    NOT NULL,
    atr                 REAL    NOT NULL,
    entry_order_id      TEXT    NOT NULL REFERENCES orders(order_id),
    entry_expires_at    TEXT    NOT NULL,                    -- IST ISO (design L1)
    stop_order_id       TEXT    REFERENCES orders(order_id),
    target_order_id     TEXT    REFERENCES orders(order_id),
    exit_order_ids      TEXT    NOT NULL DEFAULT '[]',       -- JSON: market exits / buy-backs
    position_id         TEXT    REFERENCES positions(position_id),
    state               TEXT    NOT NULL
        CHECK (state IN ('entry_pending', 'open', 'exiting', 'closed', 'cancelled')),
    exit_reason         TEXT,
    realised_pnl        REAL,
    trade_plan_id       TEXT,
    recommendation_id   TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    closed_at           TEXT,
    CHECK (stop_price < entry_price AND entry_price < target_price)   -- long only (v1)
);

CREATE INDEX idx_intraday_brackets_active ON intraday_brackets(state, session_date);
