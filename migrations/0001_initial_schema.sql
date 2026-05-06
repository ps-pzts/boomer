-- Phase 1: Capital and Risk Framework schema
-- schema_version is created by the migration runner before this runs.

CREATE TABLE capital_ledger (
    ledger_id              TEXT PRIMARY KEY,
    as_of_date             TEXT NOT NULL UNIQUE,   -- ISO date YYYY-MM-DD
    total_capital          REAL NOT NULL,
    total_cash             REAL NOT NULL,
    long_term_allocated_pct  REAL NOT NULL,
    swing_allocated_pct      REAL NOT NULL,
    intraday_allocated_pct   REAL NOT NULL,
    long_term_deployed     REAL NOT NULL DEFAULT 0.0,
    swing_deployed         REAL NOT NULL DEFAULT 0.0,
    intraday_deployed      REAL NOT NULL DEFAULT 0.0,
    high_water_mark        REAL NOT NULL,
    eod_drawdown_pct       REAL NOT NULL DEFAULT 0.0,
    consecutive_loss_days  INTEGER NOT NULL DEFAULT 0,
    peak_date              TEXT NOT NULL,           -- ISO date YYYY-MM-DD
    created_at             TEXT NOT NULL            -- ISO timestamp UTC
);

CREATE INDEX idx_capital_ledger_date ON capital_ledger(as_of_date DESC);

CREATE TABLE risk_config (
    config_id                        TEXT PRIMARY KEY,
    version                          INTEGER NOT NULL UNIQUE,
    effective_from                   TEXT NOT NULL,   -- ISO date
    -- Position sizing (% of bucket capital risked per trade)
    risk_per_intraday_trade_pct      REAL NOT NULL DEFAULT 0.005,
    risk_per_swing_trade_pct         REAL NOT NULL DEFAULT 0.010,
    risk_per_long_term_trade_pct     REAL NOT NULL DEFAULT 0.010,
    -- Track-level circuit breakers
    intraday_daily_loss_limit_pct    REAL NOT NULL DEFAULT 0.020,
    swing_weekly_loss_limit_pct      REAL NOT NULL DEFAULT 0.040,
    -- Portfolio-level circuit breakers
    portfolio_daily_loss_limit_pct   REAL NOT NULL DEFAULT 0.020,
    portfolio_weekly_loss_limit_pct  REAL NOT NULL DEFAULT 0.040,
    portfolio_max_drawdown_pct       REAL NOT NULL DEFAULT 0.080,
    -- Concentration caps
    single_stock_cap_pct             REAL NOT NULL DEFAULT 0.050,
    sector_cap_pct                   REAL NOT NULL DEFAULT 0.250,
    correlation_cluster_cap_pct      REAL NOT NULL DEFAULT 0.350,
    -- Track decay triggers
    intraday_consecutive_loss_count  INTEGER NOT NULL DEFAULT 3,
    swing_30d_loss_count             INTEGER NOT NULL DEFAULT 4,
    -- Black swan
    nifty_intraday_pause_pct         REAL NOT NULL DEFAULT 0.030,
    -- Per-track live-vs-backtest confidence haircut (initial 0.70; recalibrated after 60 live trades)
    live_backtest_ratio_long_term    REAL NOT NULL DEFAULT 0.70,
    live_backtest_ratio_swing        REAL NOT NULL DEFAULT 0.70,
    live_backtest_ratio_intraday     REAL NOT NULL DEFAULT 0.70,
    -- FinBERT confidence threshold (filings below this stored as 'unclassified')
    sentiment_confidence_threshold   REAL NOT NULL DEFAULT 0.60,
    created_at                       TEXT NOT NULL
);

-- Four self-funding funds. Balances updated on each harvest event.
CREATE TABLE funds (
    fund_type    TEXT PRIMARY KEY CHECK (fund_type IN ('ops', 'dev', 'owner', 'tax')),
    balance      REAL NOT NULL DEFAULT 0.0,
    last_updated TEXT NOT NULL
);

INSERT INTO funds (fund_type, balance, last_updated) VALUES
    ('ops',   0.0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('dev',   0.0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('owner', 0.0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('tax',   0.0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE harvest_events (
    event_id             TEXT PRIMARY KEY,
    harvest_date         TEXT NOT NULL,   -- ISO date
    pre_harvest_capital  REAL NOT NULL,
    pre_harvest_hwm      REAL NOT NULL,
    excess               REAL NOT NULL,
    harvest_amount       REAL NOT NULL,
    ops_credit           REAL NOT NULL,
    dev_credit           REAL NOT NULL,
    post_harvest_capital REAL NOT NULL,
    post_harvest_hwm     REAL NOT NULL,
    created_at           TEXT NOT NULL
);

CREATE TABLE circuit_breaker_events (
    event_id       TEXT PRIMARY KEY,
    breaker_name   TEXT NOT NULL,
    event_type     TEXT NOT NULL CHECK (event_type IN ('tripped', 'reset')),
    trip_value     REAL,
    trip_threshold REAL,
    reset_reason   TEXT,
    event_time     TEXT NOT NULL,   -- ISO timestamp UTC
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_cb_events ON circuit_breaker_events(breaker_name, event_time DESC);

CREATE TABLE capital_flow_events (
    event_id       TEXT PRIMARY KEY,
    event_date     TEXT NOT NULL,
    flow_type      TEXT NOT NULL CHECK (flow_type IN ('injection', 'withdrawal', 'harvest_withdrawal')),
    amount         REAL NOT NULL,   -- positive = inflow, negative = outflow
    hwm_adjustment REAL NOT NULL,   -- corresponding HWM change (same sign as amount)
    notes          TEXT,
    created_at     TEXT NOT NULL
);
