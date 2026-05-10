-- Phase 5: Orchestrator, Alert, and Operations schema
-- Forward-only migration — do not modify after deployment.

-- ─────────────────────────────────────────────
-- Orchestrator: bot_mode state
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bot_mode (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    mode        TEXT    NOT NULL DEFAULT 'auto',      -- auto / paused / emergency_stop
    changed_at  TEXT    NOT NULL,
    changed_by  TEXT    NOT NULL DEFAULT 'system',   -- system / operator / manual_db
    reason      TEXT
);

INSERT OR IGNORE INTO bot_mode (id, mode, changed_at, changed_by)
VALUES (1, 'auto', datetime('now'), 'system');

-- Audit log of every mode change
CREATE TABLE IF NOT EXISTS bot_mode_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    old_mode    TEXT    NOT NULL,
    new_mode    TEXT    NOT NULL,
    changed_at  TEXT    NOT NULL,
    changed_by  TEXT    NOT NULL,
    reason      TEXT
);

-- ─────────────────────────────────────────────
-- Orchestrator: task run tracking
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT    NOT NULL,
    run_date        TEXT    NOT NULL,           -- YYYY-MM-DD UTC anchor for the run
    status          TEXT    NOT NULL,           -- PENDING / RUNNING / SUCCESS / FAILED / RETRYING / FAILED_FINAL / TIMEOUT / INTERRUPTED / SKIPPED
    started_at      TEXT,
    ended_at        TEXT,
    attempt         INTEGER NOT NULL DEFAULT 1,
    manual_override INTEGER NOT NULL DEFAULT 0, -- 1 if dependency check was bypassed
    error_message   TEXT,
    error_traceback TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_runs_task_date
    ON task_runs (task_id, run_date);

CREATE INDEX IF NOT EXISTS idx_task_runs_status
    ON task_runs (status, started_at);

-- ─────────────────────────────────────────────
-- Trading calendar
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date  TEXT PRIMARY KEY,  -- YYYY-MM-DD (IST date)
    is_trading  INTEGER NOT NULL DEFAULT 1,  -- 1 = trading day, 0 = holiday
    description TEXT                          -- e.g. "Diwali", "Republic Day"
);

-- Pre-populate 2026 NSE holidays (extend yearly)
INSERT OR IGNORE INTO trading_calendar (trade_date, is_trading, description) VALUES
    ('2026-01-26', 0, 'Republic Day'),
    ('2026-03-25', 0, 'Holi'),
    ('2026-04-03', 0, 'Good Friday'),
    ('2026-04-10', 0, 'Id-ul-Fitr (Ramzan Id)'),
    ('2026-04-14', 0, 'Dr. Baba Saheb Ambedkar Jayanti'),
    ('2026-05-01', 0, 'Maharashtra Day'),
    ('2026-08-15', 0, 'Independence Day'),
    ('2026-10-02', 0, 'Mahatma Gandhi Jayanti'),
    ('2026-10-22', 0, 'Diwali Laxmi Puja'),
    ('2026-11-05', 0, 'Gurunanak Jayanti'),
    ('2026-12-25', 0, 'Christmas');

-- ─────────────────────────────────────────────
-- Alert log
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alert_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    severity        TEXT    NOT NULL,   -- INFO / WARN / CRITICAL
    title           TEXT    NOT NULL,
    body            TEXT    NOT NULL,
    sent_at         TEXT    NOT NULL,
    channels_tried  TEXT    NOT NULL,   -- JSON list: ["telegram", "email"]
    channels_ok     TEXT    NOT NULL,   -- JSON list of successful channels
    source_task_id  TEXT                -- optional: which task triggered this
);

CREATE INDEX IF NOT EXISTS idx_alert_log_severity_sent
    ON alert_log (severity, sent_at);

-- Missed CRITICAL alerts shown on next dashboard load
CREATE TABLE IF NOT EXISTS critical_notification_failures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    body            TEXT    NOT NULL,
    failed_at       TEXT    NOT NULL,
    acknowledged    INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT
);

