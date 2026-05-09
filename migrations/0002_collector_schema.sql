-- Phase 2: Collector schema
-- Two-layer storage: raw_archive (Layer 1, immutable) + parsed tables (Layer 2, rebuildable).
-- Routing rule: minute bars and bulk historical data NEVER go into SQLite — parquet lake only.
-- All timestamps stored as UTC. Trading day boundary computed from UTC+IST offset.

-- ─────────────────────────────────────────────────
-- Layer 1: Raw archive (immutable, append-only)
-- ─────────────────────────────────────────────────

CREATE TABLE raw_archive (
    raw_id          TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,                    -- ISO timestamp UTC
    request_url     TEXT NOT NULL,
    request_params  TEXT,                             -- JSON
    response_status INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,                    -- SHA-256 hex
    content_path    TEXT NOT NULL,                    -- path to gzipped payload on disk
    parser_version  TEXT,                             -- NULL until parsed
    parsed_at       TEXT,                             -- NULL until parsed
    parse_status    TEXT NOT NULL DEFAULT 'pending'
                    CHECK (parse_status IN ('pending', 'success', 'failed', 'partial'))
);

CREATE INDEX idx_raw_archive_source_fetched ON raw_archive(source, fetched_at DESC);
CREATE INDEX idx_raw_archive_hash           ON raw_archive(content_hash);
CREATE INDEX idx_raw_archive_parse_status   ON raw_archive(parse_status)
    WHERE parse_status != 'success';

-- ─────────────────────────────────────────────────
-- Layer 2: Instruments master
-- Cross-broker, cross-exchange ISIN master.
-- All joins from BSE data → NSE prices → broker tokens go through here.
-- Population: Kite instruments CSV (weekly) + NSE securities master via ISIN cross-reference.
-- ─────────────────────────────────────────────────

CREATE TABLE instruments (
    isin                    TEXT PRIMARY KEY,
    nse_symbol              TEXT,
    bse_code                TEXT,
    company_name            TEXT NOT NULL,
    kite_instrument_token   INTEGER,
    kite_tradingsymbol      TEXT,
    fyers_symbol            TEXT,                     -- NSE:SYMBOL-EQ
    series                  TEXT,
    face_value              REAL,
    last_refreshed          TEXT NOT NULL             -- ISO timestamp UTC
);

CREATE INDEX idx_instruments_nse_symbol ON instruments(nse_symbol);
CREATE INDEX idx_instruments_bse_code   ON instruments(bse_code);
CREATE INDEX idx_instruments_kite_token ON instruments(kite_instrument_token);

-- Symbol renames over time (Loophole 2: symbol changes break linkage).
CREATE TABLE symbol_history (
    history_id     TEXT PRIMARY KEY,
    old_symbol     TEXT NOT NULL,
    new_symbol     TEXT NOT NULL,
    exchange       TEXT NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    effective_date TEXT NOT NULL                      -- ISO date
);

CREATE INDEX idx_symbol_history_old ON symbol_history(old_symbol);

-- ─────────────────────────────────────────────────
-- Layer 2: Filings (SQLite)
-- ─────────────────────────────────────────────────

CREATE TABLE filings (
    filing_id            TEXT PRIMARY KEY,
    raw_id               TEXT NOT NULL REFERENCES raw_archive(raw_id),
    parser_version       TEXT NOT NULL,
    stock_symbol         TEXT NOT NULL,
    exchange             TEXT NOT NULL CHECK (exchange IN ('BSE', 'NSE')),
    filing_date          TEXT NOT NULL,               -- ISO date
    filing_time          TEXT,
    observed_at          TEXT NOT NULL,               -- UTC — point-in-time anchor
    category             TEXT NOT NULL,
        -- quarterly_results, order_win, pledging, auditor_change, fraud,
        -- promoter_buy, promoter_sell, corporate_action, agm, other
    subcategory          TEXT,
    headline             TEXT NOT NULL,
    body_summary         TEXT,                        -- first 500 chars
    attachment_url       TEXT,
    sentiment_label      TEXT
        CHECK (sentiment_label IN ('positive', 'negative', 'neutral', 'unclassified')),
    sentiment_confidence REAL,
    finbert_version      TEXT,
    is_corrected         INTEGER NOT NULL DEFAULT 0,
    corrects_filing_id   TEXT REFERENCES filings(filing_id),
    depends_on_raw_id    TEXT,                        -- dependency graph
    parse_deps_met       INTEGER NOT NULL DEFAULT 1   -- 0 = pending_dependencies
);

CREATE INDEX idx_filings_symbol_date   ON filings(stock_symbol, filing_date DESC);
CREATE INDEX idx_filings_observed_at   ON filings(observed_at DESC);
CREATE INDEX idx_filings_category      ON filings(category, observed_at DESC);
CREATE INDEX idx_filings_exchange_date ON filings(exchange, filing_date DESC);

-- ─────────────────────────────────────────────────
-- Layer 2: Bulk and block deals (SQLite + mirrored to parquet lake EOD)
-- ─────────────────────────────────────────────────

CREATE TABLE bulk_deals (
    deal_id           TEXT PRIMARY KEY,
    raw_id            TEXT NOT NULL REFERENCES raw_archive(raw_id),
    parser_version    TEXT NOT NULL,
    stock_symbol      TEXT NOT NULL,
    exchange          TEXT NOT NULL CHECK (exchange IN ('BSE', 'NSE')),
    deal_date         TEXT NOT NULL,                  -- ISO date
    observed_at       TEXT NOT NULL,                  -- UTC
    client_name       TEXT NOT NULL,
    client_normalized TEXT,
    is_smart_money    INTEGER NOT NULL DEFAULT 0,
    transaction_type  TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
    quantity          REAL NOT NULL,
    price             REAL NOT NULL,
    value             REAL NOT NULL,
    is_corrected      INTEGER NOT NULL DEFAULT 0,
    corrects_deal_id  TEXT REFERENCES bulk_deals(deal_id)
);

CREATE INDEX idx_bulk_deals_symbol_date ON bulk_deals(stock_symbol, deal_date DESC);
CREATE INDEX idx_bulk_deals_observed    ON bulk_deals(observed_at DESC);
CREATE INDEX idx_bulk_deals_smart_money ON bulk_deals(is_smart_money, deal_date DESC)
    WHERE is_smart_money = 1;

-- ─────────────────────────────────────────────────
-- Layer 2: Promoter holding changes (SQLite)
-- Stores raw share counts from SAST Reg 31.
-- holding_pct is a derived feature (Stage 0): shares_held / total_shares_outstanding.
-- ─────────────────────────────────────────────────

CREATE TABLE promoter_changes (
    change_id          TEXT PRIMARY KEY,
    raw_id             TEXT NOT NULL REFERENCES raw_archive(raw_id),
    parser_version     TEXT NOT NULL,
    stock_symbol       TEXT NOT NULL,
    exchange           TEXT NOT NULL CHECK (exchange IN ('BSE', 'NSE')),
    event_date         TEXT NOT NULL,                 -- date of the transaction
    observed_at        TEXT NOT NULL,                 -- UTC
    promoter_name      TEXT NOT NULL,
    shares_held_before REAL NOT NULL,
    shares_held_after  REAL NOT NULL,
    transaction_mode   TEXT NOT NULL
        CHECK (transaction_mode IN ('open_market', 'preferential', 'pledged', 'released_pledge', 'other')),
    regulation         TEXT NOT NULL DEFAULT 'SAST_31',
    is_corrected       INTEGER NOT NULL DEFAULT 0,
    corrects_change_id TEXT REFERENCES promoter_changes(change_id)
);

CREATE INDEX idx_promoter_changes_symbol_date ON promoter_changes(stock_symbol, event_date DESC);
CREATE INDEX idx_promoter_changes_observed    ON promoter_changes(observed_at DESC);

-- ─────────────────────────────────────────────────
-- Layer 2: Shares outstanding (SQLite)
-- Daily total issued capital from NSE CM Bhavcopy with Market Cap (TOTAL_SHARES column).
-- Required by Stage 0 to compute promoter_holding_pct from SAST raw share counts.
-- VERIFY: exact NSE URL and TOTAL_SHARES column name before deploying the fetcher.
-- ─────────────────────────────────────────────────

CREATE TABLE shares_outstanding (
    isin         TEXT NOT NULL,
    stock_symbol TEXT NOT NULL,
    exchange     TEXT NOT NULL DEFAULT 'NSE',
    trade_date   TEXT NOT NULL,                       -- ISO date
    total_shares INTEGER NOT NULL,
    observed_at  TEXT NOT NULL,                       -- UTC
    raw_id       TEXT REFERENCES raw_archive(raw_id),
    PRIMARY KEY (isin, trade_date)
);

CREATE INDEX idx_shares_outstanding_symbol_date ON shares_outstanding(stock_symbol, trade_date DESC);
CREATE INDEX idx_shares_outstanding_isin_date   ON shares_outstanding(isin, trade_date DESC);

-- ─────────────────────────────────────────────────
-- Layer 2: F&O OI daily snapshots (SQLite metadata + parquet lake for full history)
-- Source: NSE F&O bhavcopy fo_bhav_copy_*.csv.
-- Derived features (overnight_oi_change_pct, max_pain_strike, iv_percentile_252d,
-- put_call_ratio_oi, put_call_ratio_volume) computed in Stage 0 — not stored here.
-- ─────────────────────────────────────────────────

CREATE TABLE fo_oi_daily (
    record_id          TEXT PRIMARY KEY,
    raw_id             TEXT NOT NULL REFERENCES raw_archive(raw_id),
    parser_version     TEXT NOT NULL,
    underlying_symbol  TEXT NOT NULL,
    exchange           TEXT NOT NULL DEFAULT 'NSE',
    instrument_type    TEXT NOT NULL CHECK (instrument_type IN ('FUT', 'CE', 'PE')),
    expiry_date        TEXT NOT NULL,                 -- ISO date
    strike_price       REAL,                          -- NULL for futures
    trade_date         TEXT NOT NULL,                 -- ISO date
    observed_at        TEXT NOT NULL,                 -- UTC
    open_interest      INTEGER NOT NULL,
    oi_change          INTEGER,                       -- NULL on first observation
    volume             INTEGER NOT NULL DEFAULT 0,
    close_price        REAL,
    iv                 REAL,                          -- options only; NULL for futures
    is_corrected       INTEGER NOT NULL DEFAULT 0,
    corrects_record_id TEXT REFERENCES fo_oi_daily(record_id)
);

CREATE INDEX idx_fo_oi_daily_symbol_date ON fo_oi_daily(underlying_symbol, trade_date DESC);
CREATE INDEX idx_fo_oi_daily_expiry      ON fo_oi_daily(expiry_date, underlying_symbol);
CREATE INDEX idx_fo_oi_daily_trade_date  ON fo_oi_daily(trade_date DESC);

-- ─────────────────────────────────────────────────
-- Layer 2: Daily prices (SQLite — last 30 days rolling window)
-- NSE bhavcopy is primary source; broker API is cross-check for same-day EOD.
-- Historical prices beyond 30 days live in the parquet lake (prices_daily/).
-- Maintenance job prunes rows older than 30 days from SQLite after confirming parquet has them.
-- ─────────────────────────────────────────────────

CREATE TABLE prices (
    stock_symbol      TEXT NOT NULL,
    exchange          TEXT NOT NULL DEFAULT 'NSE',
    trade_date        TEXT NOT NULL,                  -- ISO date
    open              REAL NOT NULL,
    high              REAL NOT NULL,
    low               REAL NOT NULL,
    close             REAL NOT NULL,
    volume            INTEGER NOT NULL DEFAULT 0,
    value_traded      REAL,
    is_adjusted       INTEGER NOT NULL DEFAULT 0,
    adjustment_factor REAL NOT NULL DEFAULT 1.0,
    as_of_date        TEXT NOT NULL,                  -- when adjustment was last computed
    raw_id            TEXT REFERENCES raw_archive(raw_id),
    PRIMARY KEY (stock_symbol, exchange, trade_date)
);

CREATE INDEX idx_prices_symbol_date ON prices(stock_symbol, trade_date DESC);
CREATE INDEX idx_prices_date        ON prices(trade_date DESC);

-- ─────────────────────────────────────────────────
-- Layer 2: Quarterly financials (SQLite)
-- Scraped from Screener.in HTML post quarterly results filing.
-- observed_at = results_announcement_date + 2h (point-in-time correct).
-- Rate: 1 req/5s, 2–6 AM IST only. Low volume: ~50 MB after 5 years.
-- ─────────────────────────────────────────────────

CREATE TABLE quarterly_financials (
    fin_id           TEXT PRIMARY KEY,
    stock_symbol     TEXT NOT NULL,
    exchange         TEXT NOT NULL DEFAULT 'NSE',
    period_end       TEXT NOT NULL,                   -- ISO date e.g. 2024-03-31
    period_type      TEXT NOT NULL CHECK (period_type IN ('Q', 'A')),
    revenue          REAL,                            -- net sales ₹ crore
    operating_profit REAL,                            -- EBITDA/EBIT ₹ crore
    opm_pct          REAL,
    pat              REAL,                            -- profit after tax ₹ crore
    cfo              REAL,                            -- cash from operations ₹ crore
    source_url       TEXT NOT NULL,
    scraped_at       TEXT NOT NULL,                   -- UTC
    observed_at      TEXT NOT NULL,                   -- UTC = announcement_date + 2h
    raw_id           TEXT REFERENCES raw_archive(raw_id)
);

CREATE UNIQUE INDEX idx_qfin_symbol_period ON quarterly_financials(stock_symbol, period_end, period_type);
CREATE INDEX idx_qfin_observed             ON quarterly_financials(observed_at DESC);

-- ─────────────────────────────────────────────────
-- Layer 2: Index constituents history (SQLite)
-- Survivorship-bias correction: historical universe per date.
-- Backfilled from NSE methodology archives, then maintained quarterly.
-- Backtest query: WHERE effective_from <= D AND (effective_to IS NULL OR effective_to > D)
-- Eliminates ~3-5% annual return inflation from survivorship bias.
-- ─────────────────────────────────────────────────

CREATE TABLE index_constituents_history (
    constituent_id           TEXT PRIMARY KEY,        -- UUID
    index_name               TEXT NOT NULL,           -- NIFTY_50, NIFTY_500, NIFTY_BANK, etc.
    stock_symbol             TEXT NOT NULL,
    exchange                 TEXT NOT NULL DEFAULT 'NSE',
    effective_from           TEXT NOT NULL,           -- ISO date (inclusive)
    effective_to             TEXT,                    -- ISO date (exclusive); NULL = still member
    change_reason            TEXT NOT NULL
        CHECK (change_reason IN ('addition', 'removal', 'delisting', 'merger', 'rename', 'initial_load')),
    source_announcement_url  TEXT
);

CREATE INDEX idx_idx_constituents_index_from ON index_constituents_history(index_name, effective_from);
CREATE INDEX idx_idx_constituents_symbol      ON index_constituents_history(stock_symbol, index_name);

-- ─────────────────────────────────────────────────
-- Layer 2: Corporate actions (SQLite)
-- Used for retroactive price adjustment in parquet lake.
-- Low volume (few hundred rows/year), frequently joined.
-- ─────────────────────────────────────────────────

CREATE TABLE corporate_actions (
    action_id         TEXT PRIMARY KEY,               -- UUID
    raw_id            TEXT REFERENCES raw_archive(raw_id),
    parser_version    TEXT,
    stock_symbol      TEXT NOT NULL,
    exchange          TEXT NOT NULL DEFAULT 'NSE',
    action_type       TEXT NOT NULL
        CHECK (action_type IN ('split', 'bonus', 'dividend', 'rights', 'merger', 'delisting', 'name_change')),
    announcement_date TEXT NOT NULL,                  -- ISO date
    record_date       TEXT,                           -- ISO date
    ex_date           TEXT,                           -- ISO date
    observed_at       TEXT NOT NULL,                  -- UTC
    ratio_or_amount   TEXT,                           -- e.g. "1:5" for split; "2.50" for dividend
    notes             TEXT
);

CREATE INDEX idx_corporate_actions_symbol_ex    ON corporate_actions(stock_symbol, ex_date DESC);
CREATE INDEX idx_corporate_actions_type_ex      ON corporate_actions(action_type, ex_date DESC);
CREATE INDEX idx_corporate_actions_record_date  ON corporate_actions(record_date);

-- ─────────────────────────────────────────────────
-- Health / observability: collection runs
-- One row per source per fetch attempt.
-- Drives the dashboard "data health" panel.
-- ─────────────────────────────────────────────────

CREATE TABLE collection_runs (
    run_id          TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    started_at      TEXT NOT NULL,                    -- UTC
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'success', 'partial', 'failed', 'skipped')),
    records_fetched INTEGER NOT NULL DEFAULT 0,
    records_new     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_collection_runs_source_started ON collection_runs(source, started_at DESC);
CREATE INDEX idx_collection_runs_failed         ON collection_runs(status, started_at DESC)
    WHERE status IN ('failed', 'partial');

-- NOTE: prices_minute lives in the parquet lake only (/var/lib/boomer/lake/prices_minute/).
-- It is never written to SQLite. The Category D streaming writer (WebSocket aggregator)
-- writes date-partitioned parquet files directly. No SQLite table is defined here.
