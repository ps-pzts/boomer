-- #50 part 2 — NSE ASM / GSM surveillance lists. Stocks on either list are excluded from
-- the intraday universe (circuit-band cuts, trade-for-trade style restrictions, manipulation
-- risk). Written by the nightly collector; read by brain/universe.py.
--
-- list_date is the IST date the list was FETCHED (the list as published that day), not a
-- trade date: NSE publishes the current list only, so it is valid from the fetch forward.
-- The universe reads the latest list_date <= session date (point in time). A successful
-- fetch never has zero rows (the parser rejects an empty list), so MAX(list_date) per
-- list_type is also the freshness marker.

CREATE TABLE surveillance_list (
    list_date    TEXT NOT NULL,                              -- ISO date (IST) of the fetch
    list_type    TEXT NOT NULL CHECK (list_type IN ('ASM', 'GSM')),
    symbol       TEXT NOT NULL,                              -- NSE trading symbol, upper case
    isin         TEXT,                                       -- when the source carries it
    stage        TEXT,                                       -- ASM stage / GSM stage, as published
    raw_id       TEXT REFERENCES raw_archive(raw_id),
    observed_at  TEXT NOT NULL,                              -- IST wall clock
    PRIMARY KEY (list_date, list_type, symbol)
);

CREATE INDEX idx_surveillance_list_type_date ON surveillance_list(list_type, list_date DESC);
