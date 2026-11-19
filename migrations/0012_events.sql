-- SM-1 / OBS-1 (#56) — one-way event stream (designs/observability.md §4).
-- Every component WRITES here; only src/observability/ READS. Trading code never
-- reads this table to make a decision (enforced by tests/test_events_read_only.py).
-- state_transition events are inserted in the same transaction as the state change
-- they describe (designs/state-machines.md §3), so the two can never disagree.

CREATE TABLE events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,              -- IST ISO timestamp, milliseconds
    session_date    TEXT    NOT NULL,              -- IST trading date
    mode            TEXT    NOT NULL,              -- paper | live | unknown
    source          TEXT    NOT NULL,              -- emitting component
    boot_id         TEXT    NOT NULL,              -- random per process start
    source_seq      INTEGER NOT NULL,              -- monotonic per (source, boot_id)
    kind            TEXT    NOT NULL,              -- state_transition | decision | heartbeat | ...
    name            TEXT    NOT NULL,
    severity        TEXT    NOT NULL DEFAULT 'info'
        CHECK (severity IN ('debug', 'info', 'warn', 'critical')),
    entity_type     TEXT,
    entity_id       TEXT,
    cycle_id        TEXT,
    trade_id        TEXT,
    order_id        TEXT,
    symbol          TEXT,
    payload         TEXT    NOT NULL DEFAULT '{}', -- JSON
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_events_session_ts ON events(session_date, ts);
CREATE INDEX idx_events_entity     ON events(entity_type, entity_id, event_id);
CREATE INDEX idx_events_trade      ON events(trade_id) WHERE trade_id IS NOT NULL;
CREATE INDEX idx_events_kind       ON events(kind, session_date);
CREATE INDEX idx_events_source_seq ON events(source, boot_id, source_seq);
