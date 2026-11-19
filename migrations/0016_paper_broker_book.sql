-- #64 — the paper broker's book (simulated orders, MIS positions, cash), one row per
-- session day, rewritten on every change. A mid-session restart restores it, so
-- paper trades keep being managed and the reconcile-before-resume check (§4.5)
-- compares against the same book the trades were placed in.

CREATE TABLE paper_broker_book (
    session_date    TEXT    PRIMARY KEY,   -- ISO date (IST)
    state           TEXT    NOT NULL,      -- JSON: {"cash", "orders", "positions"}
    updated_at      TEXT    NOT NULL
);
