-- Migration: 0004_executor_schema
-- Phase 4 — Executor + Backtesting
-- Creates: orders, executions, positions, gtt_orders,
--          reconciliation_alerts, executor_errors,
--          backtest_runs, backtest_trades, backtest_daily_state

-- ─── Orders ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT    PRIMARY KEY,
    broker_order_id     TEXT    NOT NULL DEFAULT '',
    broker_id           TEXT    NOT NULL,   -- kite | fyers | mock | paper
    symbol              TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    side                TEXT    NOT NULL,   -- buy | sell
    order_type          TEXT    NOT NULL,   -- market | limit | sl | sl_limit
    quantity            INTEGER NOT NULL,
    filled_quantity     INTEGER NOT NULL DEFAULT 0,
    product             TEXT    NOT NULL,   -- mis | cnc
    price               REAL    NOT NULL DEFAULT 0,
    trigger_price       REAL    NOT NULL DEFAULT 0,
    average_fill_price  REAL    NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'created',
    -- created|submitting|pending|triggered|partial|filled|cancelled|rejected|expired|error
    validity            TEXT    NOT NULL DEFAULT 'day',
    idempotency_key     TEXT    NOT NULL DEFAULT '',
    tag                 TEXT    NOT NULL DEFAULT '',
    rejection_reason    TEXT    NOT NULL DEFAULT '',
    parent_order_id     TEXT,               -- for cascade-linked stop/target orders
    parent_gtt_id       TEXT,               -- gtt_orders.gtt_id that triggered this order
    trade_plan_id       TEXT,               -- brain.trade_plans.plan_id
    recommendation_id   TEXT,               -- brain.recommendations.recommendation_id
    unprotected_flag    INTEGER NOT NULL DEFAULT 0,
    unmanaged           INTEGER NOT NULL DEFAULT 0,  -- 1 = placed outside bot
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol
    ON orders(symbol, exchange, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_broker
    ON orders(broker_id, broker_order_id);

CREATE INDEX IF NOT EXISTS idx_orders_idempotency
    ON orders(idempotency_key) WHERE idempotency_key != '';

-- ─── Executions (fills) ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS executions (
    execution_id        TEXT    PRIMARY KEY,
    order_id            TEXT    NOT NULL REFERENCES orders(order_id),
    broker_execution_id TEXT    NOT NULL DEFAULT '',
    quantity            INTEGER NOT NULL,
    price               REAL    NOT NULL,
    side                TEXT    NOT NULL,
    executed_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_order
    ON executions(order_id);

-- ─── Positions ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS positions (
    position_id         TEXT    PRIMARY KEY,
    symbol              TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    track               TEXT    NOT NULL,  -- intraday | swing | long_term
    bucket_id           TEXT    NOT NULL,  -- capital bucket
    broker_id           TEXT    NOT NULL,
    quantity            INTEGER NOT NULL,
    average_entry_price REAL    NOT NULL,
    current_price       REAL    NOT NULL DEFAULT 0,
    unrealised_pnl      REAL    NOT NULL DEFAULT 0,
    realised_pnl        REAL    NOT NULL DEFAULT 0,
    stop_loss_price     REAL    NOT NULL,
    target_price        REAL    NOT NULL,
    atr_at_entry        REAL    NOT NULL,
    entry_order_id      TEXT    NOT NULL REFERENCES orders(order_id),
    gtt_oco_id          TEXT,               -- gtt_orders.gtt_id for the active OCO stop
    unprotected_flag    INTEGER NOT NULL DEFAULT 0,
    unprotected_since   TEXT,               -- UTC ISO; set when stop is missing
    unmanaged           INTEGER NOT NULL DEFAULT 0,
    health_score        REAL    NOT NULL DEFAULT 100,
    is_open             INTEGER NOT NULL DEFAULT 1,
    entry_at            TEXT    NOT NULL,
    exit_at             TEXT,
    trade_plan_id       TEXT,
    recommendation_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_open
    ON positions(is_open, track, entry_at DESC);

CREATE INDEX IF NOT EXISTS idx_positions_symbol
    ON positions(symbol, exchange, is_open);

-- ─── GTT Orders ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gtt_orders (
    gtt_id              TEXT    PRIMARY KEY,
    broker_gtt_id       TEXT    NOT NULL DEFAULT '',
    broker_id           TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    gtt_type            TEXT    NOT NULL,  -- single | oco
    trigger_price       REAL    NOT NULL DEFAULT 0,   -- single-leg trigger
    limit_price         REAL    NOT NULL DEFAULT 0,   -- single-leg limit
    sl_trigger_price    REAL    NOT NULL DEFAULT 0,   -- OCO SL leg trigger
    sl_limit_price      REAL    NOT NULL DEFAULT 0,   -- OCO SL leg limit
    target_trigger_price REAL   NOT NULL DEFAULT 0,   -- OCO target leg trigger
    target_limit_price  REAL    NOT NULL DEFAULT 0,   -- OCO target leg limit
    quantity            INTEGER NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'gtt_active',
    -- gtt_active|gtt_triggered|gtt_cancelled|gtt_expired|gtt_deleted
    parent_order_id     TEXT,               -- entry order this GTT OCO protects
    triggered_order_id  TEXT,               -- orders.order_id created when triggered
    valid_until         TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    last_checked_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gtt_orders_active
    ON gtt_orders(status, last_checked_at);

CREATE INDEX IF NOT EXISTS idx_gtt_orders_symbol
    ON gtt_orders(symbol, exchange, status);

CREATE INDEX IF NOT EXISTS idx_gtt_orders_broker
    ON gtt_orders(broker_id, broker_gtt_id);

-- ─── Reconciliation Alerts ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reconciliation_alerts (
    alert_id            TEXT    PRIMARY KEY,
    broker_id           TEXT    NOT NULL,
    alert_type          TEXT    NOT NULL,
    -- position_bot_only | position_broker_only | gtt_missing | quantity_mismatch
    -- | price_mismatch | eod_cash_mismatch
    symbol              TEXT,
    exchange            TEXT,
    bot_value           TEXT,   -- JSON; what bot thinks
    broker_value        TEXT,   -- JSON; what broker reports
    resolved            INTEGER NOT NULL DEFAULT 0,
    resolved_at         TEXT,
    resolution_note     TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recon_alerts_open
    ON reconciliation_alerts(resolved, created_at DESC);

-- ─── Executor Errors ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS executor_errors (
    error_id            TEXT    PRIMARY KEY,
    broker_id           TEXT,
    error_type          TEXT    NOT NULL,
    -- api_timeout | auth_failure | rate_limit | order_rejected | gtt_rejected
    -- | connection_lost | state_machine_violation | unknown
    order_id            TEXT,
    gtt_id              TEXT,
    message             TEXT    NOT NULL,
    context             TEXT,   -- JSON; raw broker response or traceback
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executor_errors_type
    ON executor_errors(error_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_executor_errors_broker
    ON executor_errors(broker_id, created_at DESC);

-- ─── Backtesting ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              TEXT    PRIMARY KEY,
    name                TEXT    NOT NULL,
    code_hash           TEXT    NOT NULL,   -- SHA-256 of code + config for holdout tracking
    start_date          TEXT    NOT NULL,
    end_date            TEXT    NOT NULL,
    initial_capital     REAL    NOT NULL,
    final_capital       REAL    NOT NULL DEFAULT 0,
    total_return_pct    REAL,
    annualised_return_pct REAL,
    sharpe_ratio        REAL,
    max_drawdown_pct    REAL,
    win_rate            REAL,
    avg_win_pct         REAL,
    avg_loss_pct        REAL,
    expectancy          REAL,
    total_trades        INTEGER NOT NULL DEFAULT 0,
    universe            TEXT    NOT NULL DEFAULT 'nifty500_current',  -- survivorship bias note
    tracks              TEXT    NOT NULL DEFAULT '["long_term","swing","intraday"]',  -- JSON
    status              TEXT    NOT NULL DEFAULT 'running',  -- running | complete | failed
    created_at          TEXT    NOT NULL,
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_created
    ON backtest_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_trades (
    trade_id            TEXT    PRIMARY KEY,
    run_id              TEXT    NOT NULL REFERENCES backtest_runs(run_id),
    symbol              TEXT    NOT NULL,
    track               TEXT    NOT NULL,
    side                TEXT    NOT NULL,   -- long | short
    entry_date          TEXT    NOT NULL,
    exit_date           TEXT,
    entry_price         REAL    NOT NULL,
    exit_price          REAL,
    quantity            INTEGER NOT NULL,
    gross_pnl           REAL,
    transaction_costs   REAL,
    slippage_cost       REAL,
    net_pnl             REAL,
    hold_days           INTEGER,
    exit_reason         TEXT,   -- stop_hit | target_hit | thesis_broken | forced | time_based
    signal_confidence   REAL,
    strategy_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_bt_trades_run
    ON backtest_trades(run_id, entry_date);

CREATE INDEX IF NOT EXISTS idx_bt_trades_symbol
    ON backtest_trades(symbol, track, entry_date);

CREATE TABLE IF NOT EXISTS backtest_daily_state (
    state_id            TEXT    PRIMARY KEY,
    run_id              TEXT    NOT NULL REFERENCES backtest_runs(run_id),
    date                TEXT    NOT NULL,
    total_capital       REAL    NOT NULL,
    deployed_capital    REAL    NOT NULL,
    cash                REAL    NOT NULL,
    open_positions      INTEGER NOT NULL,
    drawdown_from_hwm   REAL    NOT NULL,
    regime              TEXT    NOT NULL,
    UNIQUE(run_id, date)
);

CREATE INDEX IF NOT EXISTS idx_bt_daily_run
    ON backtest_daily_state(run_id, date);
