-- #85 — daily Kite pre-flight (08:55) and order canary (09:15) results. One row
-- per run (history kept); the latest run of each check for the session day gates
-- the Session machine: no TRADING until both passed today (designs/state-machines.md §4).

CREATE TABLE kite_daily_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date    TEXT    NOT NULL,                    -- ISO date (IST)
    check_name      TEXT    NOT NULL CHECK (check_name IN ('preflight', 'canary')),
    ok              INTEGER NOT NULL CHECK (ok IN (0, 1)),
    detail_json     TEXT    NOT NULL,                    -- CheckReport.as_dict()
    source          TEXT    NOT NULL,                    -- scheduled | operator | test
    ran_at          TEXT    NOT NULL                     -- IST wall clock
);

CREATE INDEX idx_kite_daily_checks_day ON kite_daily_checks (session_date, check_name, id);
