-- Migration: 0003_brain_schema
-- Phase 3 — System 2 Brain
-- Creates: features, signals, trade_plans, recommendations,
--          recommendation_outcomes, sector_classifications

-- ─── Stage 0: Feature Store ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS features (
    feature_id          TEXT    PRIMARY KEY,
    stock_symbol        TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    feature_name        TEXT    NOT NULL,
    feature_value       REAL    NOT NULL,
    feature_metadata    TEXT,                  -- JSON; raw inputs used
    valid_from          TEXT    NOT NULL,       -- ISO date; "as-of" date
    valid_to            TEXT,                   -- NULL = current; set when superseded
    source_max_observed_at TEXT NOT NULL,       -- latest observed_at of underlying data
    computer_version    TEXT    NOT NULL DEFAULT '1.0',
    computed_at         TEXT    NOT NULL        -- UTC ISO timestamp
);

-- Q3-3: critical indexes for morning batch query performance
-- Primary pattern: latest feature for a stock + name on a given date
CREATE INDEX IF NOT EXISTS idx_features_stock_name_valid
    ON features(stock_symbol, feature_name, valid_from DESC);

-- Point-in-time filter: exclude features from the future
CREATE INDEX IF NOT EXISTS idx_features_observed
    ON features(source_max_observed_at);

-- Bulk query: all current features for a stock (morning batch)
CREATE INDEX IF NOT EXISTS idx_features_stock_valid
    ON features(stock_symbol, valid_from DESC);

-- ─── Stage 1: Sector Classifications (manually curated) ────────────────────

CREATE TABLE IF NOT EXISTS sector_classifications (
    symbol              TEXT    NOT NULL,
    exchange            TEXT    NOT NULL DEFAULT 'NSE',
    sector              TEXT    NOT NULL,
    industry            TEXT,
    source              TEXT    NOT NULL DEFAULT 'NSE',  -- 'NSE' or 'manual_override'
    effective_from      TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    PRIMARY KEY (symbol, exchange, effective_from)
);

CREATE INDEX IF NOT EXISTS idx_sector_symbol
    ON sector_classifications(symbol, exchange, effective_from DESC);

-- ─── Stage 2: Signals ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS signals (
    signal_id           TEXT    PRIMARY KEY,
    stock_symbol        TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    track               TEXT    NOT NULL,   -- long_term | swing | intraday
    direction           TEXT    NOT NULL,   -- long | short | neutral
    raw_score           REAL    NOT NULL,   -- -1.0 to +1.0
    confidence          REAL    NOT NULL,   -- 0.0 to 1.0
    regime_at_signal    TEXT    NOT NULL,
    contributing_signals TEXT   NOT NULL,  -- JSON array of attribution
    feature_snapshot    TEXT    NOT NULL,  -- JSON of exact feature values used
    generated_at        TEXT    NOT NULL,   -- UTC ISO timestamp
    generator_version   TEXT    NOT NULL DEFAULT '1.0'
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_track
    ON signals(stock_symbol, exchange, track, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_signals_generated
    ON signals(generated_at DESC);

-- ─── Stage 3: Trade Plans ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS trade_plans (
    plan_id             TEXT    PRIMARY KEY,
    signal_id           TEXT    NOT NULL REFERENCES signals(signal_id),
    stock_symbol        TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    track               TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    entry_zone_low      REAL    NOT NULL,
    entry_zone_high     REAL    NOT NULL,
    stop_loss_price     REAL    NOT NULL,
    target_price        REAL    NOT NULL,
    expected_reward_per_share REAL NOT NULL,
    expected_risk_per_share   REAL NOT NULL,
    reward_to_risk      REAL    NOT NULL,
    expected_value_per_share  REAL NOT NULL,
    decision            TEXT    NOT NULL,   -- proceed | skip
    skip_reason         TEXT,               -- NULL if proceed
    entry_strategy_id   TEXT,               -- LT1/LT2/LT3/SW1/SW2/SW3/ID1/ID2/ID3
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trade_plans_signal
    ON trade_plans(signal_id);

CREATE INDEX IF NOT EXISTS idx_trade_plans_symbol
    ON trade_plans(stock_symbol, exchange, track, created_at DESC);

-- ─── Stage 5: Recommendations ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id   TEXT    PRIMARY KEY,
    plan_id             TEXT    NOT NULL REFERENCES trade_plans(plan_id),
    signal_id           TEXT    NOT NULL REFERENCES signals(signal_id),
    stock_symbol        TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    track               TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    -- Entry parameters (modifiable by operator for long-term)
    entry_zone_low      REAL    NOT NULL,
    entry_zone_high     REAL    NOT NULL,
    stop_loss_price     REAL    NOT NULL,
    target_price        REAL    NOT NULL,
    position_size_shares INTEGER NOT NULL,
    entry_strategy_id   TEXT,
    -- APM routing
    requires_human      INTEGER NOT NULL DEFAULT 0,  -- 1 = long-term
    -- Status lifecycle
    status              TEXT    NOT NULL DEFAULT 'generated',
    -- generated | awaiting_human | approved_by_apm | rejected_by_apm
    -- | queued_for_execution | submitted_to_broker | filled | partial_fill
    -- | rejected_by_broker | position_open | position_closed | outcome_recorded
    decision_reason     TEXT,
    -- Operator modification tracking
    operator_modified   INTEGER NOT NULL DEFAULT 0,
    original_params     TEXT,       -- JSON snapshot before modification
    -- Attribution
    portfolio_impact    TEXT,       -- JSON: sector_after, concentration_after
    -- Timestamps (all UTC)
    generated_at        TEXT    NOT NULL,
    decided_at          TEXT,
    queued_at           TEXT,
    submitted_at        TEXT,
    filled_at           TEXT,
    closed_at           TEXT,
    outcome_recorded_at TEXT,
    -- Outcome
    realised_pnl        REAL,
    actual_hold_days    INTEGER,
    intent              TEXT        -- long_term | swing | intraday
);

CREATE INDEX IF NOT EXISTS idx_recommendations_symbol_track
    ON recommendations(stock_symbol, exchange, track, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_recommendations_status
    ON recommendations(status, generated_at DESC);

-- ─── Recommendation Outcomes (cooldown tracking, Loophole 3) ───────────────

CREATE TABLE IF NOT EXISTS recommendation_outcomes (
    outcome_id          TEXT    PRIMARY KEY,
    recommendation_id   TEXT    NOT NULL REFERENCES recommendations(recommendation_id),
    stock_symbol        TEXT    NOT NULL,
    exchange            TEXT    NOT NULL,
    track               TEXT    NOT NULL,
    outcome             TEXT    NOT NULL,
    -- approved_position_opened | rejected_by_operator | expired | rejected_by_apm
    recorded_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rec_outcomes_symbol_track
    ON recommendation_outcomes(stock_symbol, exchange, track, recorded_at DESC);

