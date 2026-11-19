-- #103 — Telegram console. (1) The session records WHY it is halted (the failed
-- reconcile checks, the breaker, the Kite check) so /halt can show it without reading
-- the events table, which only src/observability may read. (2) The live trade feed
-- remembers the last event it sent, so a restart neither repeats nor skips messages.

ALTER TABLE trading_sessions ADD COLUMN halt_detail TEXT;   -- JSON, set with halt_reason

CREATE TABLE telegram_feed_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id   INTEGER NOT NULL,
    updated_at      TEXT    NOT NULL                        -- IST wall clock
);
