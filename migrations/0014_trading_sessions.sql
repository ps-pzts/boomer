-- SM-3a (#63) — one row per trading day: the Session machine's state table
-- (designs/state-machines.md §4). Holidays and weekends get no row.
--
-- state: pre_open -> connected -> ready -> trading <-> halted -> reconciling -> ...
--        -> no_new_entries -> squaring_off -> closed      (terminal)
--        pre_open / connected / ready -> abandoned        (terminal, never traded)

CREATE TABLE trading_sessions (
    session_date    TEXT    PRIMARY KEY,                 -- ISO date (IST)
    mode            TEXT    NOT NULL CHECK (mode IN ('paper', 'live')),
    state           TEXT    NOT NULL
        CHECK (state IN ('pre_open', 'connected', 'ready', 'trading', 'halted',
                         'reconciling', 'no_new_entries', 'squaring_off',
                         'closed', 'abandoned')),
    halt_reason     TEXT,                                -- set while halted (§4.3)
    halt_acked_at   TEXT,                                -- operator ack of reconcile_mismatch
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
