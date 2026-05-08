# Phase 2 — Collector (System 1)

## Goal

The collector is the system's perception layer. It ingests raw market and corporate data from external sources reliably and stores it so that downstream systems can query as point-in-time correct features.

Every assumption made downstream — about regime, about signals, about decisions — collapses if the collector lies. The collector's job description is more about *honesty and reliability* than about cleverness.

## Inputs and outputs

**Inputs (from the world):**
- BSE corporate filings (announcements, results, regulatory disclosures)
- NSE corporate filings (same categories, different format)
- Bulk and block deals (BSE + NSE)
- Insider trading disclosures (SAST regulation 7 reports)
- Promoter holding changes (SAST regulation 31)
- Daily OHLCV price data (from broker API, not scraped)
- Index data (Nifty 50, Nifty 500, sectoral indices, India VIX)
- Trading calendar (holidays, special sessions)

**Outputs (to other components):**
- Raw archive: every fetched payload, untransformed, with timestamps
- Normalised tables: parsed records with stable schemas
- Point-in-time view: any historical query returns "data as known on date X"
- Health signals: which sources succeeded, which failed, latency, freshness

The collector does not generate signals, does not make decisions, does not know what an "intraday trade" is. It answers exactly one question: *what was the world telling us on date X?*

## Time model

Two valid approaches were considered:

**Option A — Latest snapshot:** store only the current state. Simpler, faster, what most retail bots do.

**Option B — Append-only event log:** store every observed change with timestamps. Honest about what changed and when.

**Decision: Option B (append-only).** This enables backtesting with point-in-time correctness and survives parser bugs / source corrections gracefully. The complexity overhead is small if set up from day one but a miserable refactor later.

## Two-layer storage model

### Layer 1 — Raw archive (immutable, append-only)

Every payload kept exactly as received. Gzipped on disk. Indexed by `(source, fetched_at, content_hash)`. Never deleted, never edited. Single source of truth.

### Layer 2 — Normalised tables

Parsed records with stable schemas. Each row tagged with source `raw_id`, `parser_version`, `event_date`, `observed_at`. Can be wiped and rebuilt entirely from Layer 1.

### Why two layers

1. **Parsers are wrong, often.** Six months in, a parser bug will be found. With Layer 1 intact, the parser is fixed and re-run. Without it, those records are lost.

2. **Sources change format silently.** When a source breaks the parser, Layer 1 captures the actual response so a new parser can be written from real data.

3. **Audit trail.** When a trade is made on "Promoter X bought 0.5%," the exact filing PDF can be pointed to.

4. **Backtesting requires re-parsing.** Today's parser version may differ from past parser version; re-parsing the raw archive with today's parser produces a consistent dataset.

5. **Storage is cheap, regret is expensive.** Raw archive grows ~5-10 GB/year. Re-fetching historical data is often impossible (NSE doesn't allow backfetching corrections).

The cost is small: two tables instead of one, a `parser_version` field, a re-run script. The benefit is large: audit, replayability, parser-bug resilience.

## Storage targets — operational SQLite + historical parquet lake

Layer 2 normalised data is split across two storage targets based on access pattern. This is a critical early decision: putting historical bulk data into SQLite means either slow backtests or a painful migration later.

### Operational store — SQLite (`/var/lib/boomer/boomer.db`)

For data the live system reads and writes during a trading day:

- All transactional state (orders, positions, signals, recommendations, capital state, breakers, runs)
- Layer 2 tables for *event-driven* small-volume data (filings, bulk deals, promoter changes, corporate actions)
- Recent-window views (last 30 days of daily prices for fast intraday access)

Properties:
- Row-oriented, optimised for transactional OLTP
- WAL mode for concurrent reads while writes happen
- Single file, easy to back up and replicate
- Stays under ~1 GB indefinitely if disciplined about what goes in it

### Historical lake — Parquet files queried via DuckDB (`/var/lib/boomer/lake/`)

For bulk historical data the backtester and feature computer scan:

- Full historical daily prices (20+ years)
- All minute bars (forward-collected, accumulating from day one of bot operation)
- Full historical F&O OI (daily and intraday)
- Full historical bulk deals (mirrored from operational SQLite for analytical queries)
- Reference index to raw archive

Partitioning scheme:

```
/var/lib/boomer/lake/
  prices_daily/      year=YYYY/month=MM/data.parquet
  prices_minute/     year=YYYY/month=MM/day=DD/data.parquet
  fo_oi_daily/       year=YYYY/month=MM/data.parquet
  fo_oi_minute/      year=YYYY/month=MM/day=DD/data.parquet
  bulk_deals/        year=YYYY/month=MM/data.parquet
  filings_history/   year=YYYY/month=MM/data.parquet
```

DuckDB supports partition pruning natively — a query for January 2024 reads one file, not the year. Queries scale sub-linearly with dataset size.

Properties:
- Columnar storage, optimised for analytical scans
- Compression typically 3-5x for financial data
- DuckDB queries are 10-100x faster than SQLite for the workloads the backtester does
- Free, in-process (like SQLite — no server to administer)

### Routing rules

| Data | SQLite | Parquet lake |
|------|--------|--------------|
| Capital state, positions, orders | ✓ | |
| Signals, recommendations | ✓ | |
| Filings, promoter changes (events) | ✓ | mirror after EOD |
| Bulk/block deals (today) | ✓ | mirror after EOD |
| Daily prices (last 30 days) | ✓ (recent window) | ✓ (full history) |
| Daily prices (older) | | ✓ |
| Minute bars (any age) | never | ✓ |
| F&O OI (any age) | metadata only | ✓ |
| Backtest reads | never | ✓ |
| Live signal reads | ✓ | sometimes (feature computation) |

**Decision: minute bars never go into SQLite, even at v1.** Tempting "just to start" but the migration pain at month 6 is severe. Direct to parquet from day one.

### Why this split

The single most common backtester performance failure in retail bots is putting historical price data in row-oriented storage. SQLite is the wrong shape for "give me close prices for these 500 stocks across 5 years" — it loads entire rows including columns the query doesn't need, page after page. Backtests that should take seconds take hours.

Columnar storage (parquet) reads only the columns needed. DuckDB pushes filters down into the parquet read, so partition pruning eliminates whole files from consideration. The combination makes 5-year backtests across hundreds of stocks complete in seconds rather than hours.

This is also the difference between *"explore 50 hypotheses in a weekend"* and *"explore 5 hypotheses in a weekend"* during signal tuning — the iteration speed dominates everything else once data scale grows.

### What never to put in SQLite

A short list of traps:

- Minute bars (any volume)
- Tick data (if ever collected)
- Historical OHLCV beyond a recent window
- Anything with > ~10 million rows total

If a future feature needs one of these, the parquet lake is the home. Operational SQLite stays small and fast forever.

## Free-source data strategy

Historical backfill and ongoing data acquisition uses free public sources, not paid broker APIs. Kite Connect's historical data is a paid endpoint not needed for backfill — NSE/BSE publish authoritative historical archives directly, and Kite is itself derived from these.

### Layer 1 — NSE/BSE bhavcopy archives (primary)

One-time backfill of 20-25 years for:
- Daily OHLCV for all NSE equities (and BSE if desired)
- Daily F&O bhavcopy from 2001
- Daily bulk and block deals
- Daily index values
- Corporate actions (splits, bonuses, dividends, mergers)

Sources:
- NSE: `nsearchives.nseindia.com/products/content/sec_bhavdata_full_*.csv` and equivalents for F&O
- BSE: `bseindia.com/markets/MarketInfo/BhavCopy.aspx` and equivalents

Format messiness is real (NSE has changed bhavcopy formats multiple times across decades) but stable enough to script. Stored in raw archive (Layer 1) and parsed into the parquet lake.

This is the **primary source.** Authoritative, complete, free, point-in-time correct.

### Layer 2 — Yahoo Finance via `yfinance` (verification)

Weekly cross-check job: pull last 30 days from Yahoo for ~50 sample stocks, compare against bhavcopy-derived data. Divergence > 0.1% gets flagged.

Catches:
- Bugs in bhavcopy parser
- Missing corporate action adjustments
- Yahoo's own bugs (when Yahoo disagrees with NSE, you see it)

Yahoo carries survivorship bias (delisted stocks aren't there) and has occasional data quality issues, so it's a *secondary verification source*, not a primary. Cheap insurance.

### Layer 3 — Daily incremental from NSE directly (live forward updates)

For ongoing daily updates after the historical backfill:
- Daily bhavcopy in the nightly 02:00 collector run
- Daily corporate filings via existing scrapers
- Daily bulk deals from NSE archive page
- Daily F&O bhavcopy

Direct bhavcopy URLs are stable enough that wrapper libraries (NSEpy, nsetools, jugaad-data) add maintenance burden without proportional value. Direct download is simpler and more reliable.

### Layer 4 — Fundamentals (separate path)

Quarterly fundamentals are not in any of the above sources.

Backfill: scrape Screener.in for ~10 years of quarterly results across the tradeable universe (~500 stocks × 40 quarters). Polite scraping, ~1 request per 5 seconds, runs over a few days. Cross-reference with XBRL filings on a sample for verification.

Ongoing: parse new quarterly results from the filings stream as they're published.

Stored in operational SQLite (low volume; ~50 MB after 5 years).

### Layer 5 — Kite Connect for live data only

When the bot is trading live, Kite is used for:
- Real-time tick data (WebSocket)
- Order placement and modification
- Live OHLCV for held positions
- F&O live quotes for the intraday cycle

Kite's ₹2,000/month subscription is for *live* data and execution access, not historical backfill. Historical data needs are met by free sources above.

### Forward-collection principle

Today's collected minute bars become tomorrow's history. The bot's daily operation incrementally grows the parquet lake with one trading day's data per trading day. After a year of operation, a year of minute data exists for backtesting strategies that need it. After two years, two years.

This is the "data accumulates without effort" pattern. The one-time backfill establishes the historical depth; daily operation extends it forward indefinitely.

For F&O specifically: historical daily OI from bhavcopy (2001+) plus forward minute-level OI snapshots collected during live operation. Future F&O strategies have data to backtest against from day one of consideration, not day one of new collection.

## Source taxonomy

Sources have very different behaviours; mixing them up causes bugs.

### Category A — Daily snapshot sources

Provide "the state as of end of day." Polled once daily after market close.

- Bulk deals (BSE + NSE) — published EOD around 6 PM
- Block deals (BSE + NSE) — published EOD around 6 PM
- Daily OHLCV (NSE bhavcopy primary, broker API secondary) — official close around 4 PM
- Index values — same
- **Daily F&O Open Interest** (NSE F&O bhavcopy) — published post-settlement, around 6 PM
- Trading calendar — yearly, weekly verification

Pattern: one fetch per day per source. Hash response. If hash matches yesterday's *and* there should be data (trading day, non-zero volume expected), flag suspicious.

### Category B — Event stream sources

Produce events through the day. Polled at intervals during business hours.

- BSE corporate announcements
- NSE corporate announcements
- Insider trading disclosures
- Promoter holding changes

Pattern: poll every 30 minutes from 9 AM to 6 PM IST. Each fetch returns latest N items; dedupe against already-seen.

### Category C — On-demand / static sources

Fetched only when needed.

- Stock master list (active equity universe) — refresh weekly
- Sector classifications — refresh monthly
- F&O lot sizes — refresh monthly when changes published
- Index constituents — refresh quarterly
- Index constituents history — backfilled from NSE methodology archives, then maintained quarterly
- Quarterly financials (Screener.in HTML) — per company after quarterly results filing; 1 req/5s, 2–6 AM IST only
- Instrument master (Kite + Fyers instrument CSV) — weekly refresh; maps instrument tokens, ISINs, symbols across brokers

### Category D — Live streaming sources

Continuous streams during market hours. Subscribed via broker WebSocket, written directly to the parquet lake.

- Minute bars for actively held stocks and watchlist (per-minute OHLCV, accumulated forward)
- Minute-level F&O OI snapshots for actively traded contracts
- Live quotes (cached briefly for intraday cycle, not persisted)

Pattern: subscribe at market open, write per-minute aggregates to date-partitioned parquet, unsubscribe at market close. EOD reconciliation verifies expected row counts.

Volume note: ~2,000 stocks × 375 minutes per trading day = ~750k rows/day at full coverage. Compressed parquet handles this trivially. Operational SQLite never sees this data.

## Fetcher anatomy (one design, applied uniformly)

Every fetcher (one per source) follows the same internal structure. This discipline is what makes the collector maintainable.

```
Fetcher class with five methods:

1. fetch_url() — produces the URL to call (date-parameterized)
2. transport() — the HTTP call with timeout, retry, headers
3. validate() — check response is well-formed (not error page, has expected fields)
4. archive() — write raw payload to Layer 1 with metadata
5. parse() — produce Layer 2 records (called separately, not always immediately)
```

Each method is independently testable. HTTP doesn't know about parsing. Parser doesn't know about HTTP.

## Layer 1 schema (raw archive metadata)

| Field | Purpose |
|-------|---------|
| `raw_id` | UUID, primary key |
| `source` | Enum: `bse_filings`, `nse_bulk_deals`, etc. |
| `fetched_at` | UTC timestamp when received |
| `request_url` | The exact URL called |
| `request_params` | JSON of any params/headers |
| `response_status` | HTTP status |
| `content_hash` | SHA-256 of response body |
| `content_path` | Path to gzipped payload on disk |
| `parser_version` | Which parser version processed it (NULL if unprocessed) |
| `parsed_at` | When parsing succeeded (NULL if not yet) |
| `parse_status` | `pending`, `success`, `failed`, `partial` |

The `content_hash` is critical: if today's response is byte-identical to yesterday's, no re-parse needed.

## Layer 2 schemas

### `filings` table (SQLite)

| Field | Purpose |
|-------|---------|
| `filing_id` | UUID |
| `raw_id` | FK to raw archive |
| `parser_version` | e.g., "v2.1" |
| `stock_symbol` | NSE/BSE symbol |
| `exchange` | `BSE` or `NSE` |
| `filing_date` | Date the filing was made |
| `filing_time` | If available |
| `observed_at` | When *we* first saw it (point-in-time anchor) |
| `category` | Enum: `quarterly_results`, `order_win`, `pledging`, `auditor_change`, etc. |
| `subcategory` | Free text refinement |
| `headline` | Short text |
| `body_summary` | First 500 chars |
| `attachment_url` | Link to PDF if any |
| `sentiment_label` | `positive`, `negative`, `neutral`, `unclassified` |
| `sentiment_confidence` | 0.0 to 1.0 |
| `finbert_version` | Model version used for sentiment inference |
| `is_corrected` | Boolean |
| `corrects_filing_id` | If corrected, which filing it replaces |

The `observed_at` is the magical field. Backtest queries use `WHERE observed_at <= simulation_date`.

### `bulk_deals` table (SQLite + mirrored to parquet lake)

| Field | Purpose |
|-------|---------|
| `deal_id` | UUID |
| `raw_id`, `parser_version` | Provenance |
| `stock_symbol`, `exchange` | What was traded |
| `deal_date` | When the deal happened |
| `observed_at` | When we saw it |
| `client_name` | Buyer/seller name |
| `client_normalized` | Standardised name |
| `is_smart_money` | Flag if matches tracked-investors list |
| `transaction_type` | `BUY` or `SELL` |
| `quantity` | Shares |
| `price` | Per share |
| `value` | quantity × price |
| `is_corrected`, `corrects_deal_id` | Correction pattern |

### `promoter_changes` table (SQLite)

Similar pattern with `shares_held_before`, `shares_held_after` (raw share counts from SAST Reg 31), `transaction_mode` (`open_market`, `preferential`, `pledged`, `released_pledge`), and `event_date` separate from `observed_at`.

**Promoter percentage calculation:** SAST Regulation 31 filings disclose raw share counts held by promoters. To compute `holding_pct`, the system needs total shares outstanding:

```
promoter_holding_pct = (sum of promoter shares from SAST) / total_shares_outstanding
```

`total_shares_outstanding` comes from the `shares_outstanding` table (NSE CM Bhavcopy with Market Cap, TOTAL_SHARES column), joined on ISIN. This is computed at feature time in Stage 0, not stored in `promoter_changes` itself.

### `shares_outstanding` table (SQLite)

Daily total issued capital per company, derived from the NSE CM Bhavcopy with Market Cap file.

| Field | Purpose |
|-------|---------|
| `isin` | ISIN (primary key with date) |
| `stock_symbol`, `exchange` | NSE symbol |
| `trade_date` | Date this count applies to |
| `total_shares` | Total issued capital (from NSE bhavcopy TOTAL_SHARES column) |
| `observed_at` | When we fetched this |

**VERIFY before implementing fetcher:** exact NSE URL, filename pattern, and `TOTAL_SHARES` column name. Download and inspect the actual NSE "CM Bhavcopy with Market Cap" file before coding the parser.

### `fo_oi_daily` table (SQLite metadata + parquet lake for bulk)

End-of-day F&O Open Interest snapshots. Required by intraday signal track (overnight OI build-up direction, max pain proximity, IV percentile). Source: NSE F&O bhavcopy `fo_bhav_copy_*.csv`.

| Field | Purpose |
|-------|---------|
| `record_id` | UUID |
| `raw_id` | FK to raw archive |
| `parser_version` | Version that produced this row |
| `underlying_symbol` | Stock symbol of the underlying (e.g., RELIANCE) |
| `instrument_type` | `FUT`, `CE`, `PE` |
| `expiry_date` | Contract expiry |
| `strike_price` | NULL for futures, set for options |
| `trade_date` | Date this OI snapshot represents |
| `observed_at` | When *we* observed it (point-in-time anchor) |
| `open_interest` | Total OI at EOD |
| `oi_change` | Change vs prior trading day OI (NULL on first observation) |
| `volume` | Day's traded volume in this contract |
| `close_price` | Settlement / closing price |
| `iv` | Implied volatility (options only; NULL for futures) |
| `is_corrected`, `corrects_record_id` | Correction pattern |

Derived features computed from this table (Stage 0 of System 2):
- `overnight_oi_change_pct` — used by intraday "OI build-up direction" signal
- `max_pain_strike` — computed from option-chain OI distribution per expiry
- `iv_percentile_252d` — current IV vs trailing 252-day distribution
- `put_call_ratio_oi`, `put_call_ratio_volume` — sentiment indicators

**Freshness SLA:** by 8 AM next day (nightly 02:00 collector run).

### `prices` table (SQLite — last 30 days only)

| Field | Purpose |
|-------|---------|
| `stock_symbol`, `exchange`, `trade_date` | Identity |
| `open`, `high`, `low`, `close` | OHLC |
| `volume` | Total traded shares |
| `value_traded` | Total ₹ |
| `is_adjusted` | Boolean — adjusted for corporate actions? |
| `adjustment_factor` | If adjusted, cumulative factor |
| `as_of_date` | When this row's adjustment was last computed |

Historical prices beyond 30 days live in the parquet lake (`prices_daily/`). SQLite holds only a recent rolling window for fast intraday access (live capital view, same-day checks). Batch maintenance job prunes rows older than 30 days from SQLite and confirms parquet has them.

### `prices_minute` (parquet lake only — never SQLite)

Stores per-minute OHLCV bars during market hours. Source: broker WebSocket (Kite Ticker), aggregated to 1-minute resolution. Forward-collection only from day one of bot operation.

| Field | Purpose |
|-------|---------|
| `stock_symbol`, `exchange` | Identity |
| `trade_date`, `bar_minute` | Date and minute (e.g., 2024-04-22, 09:32) |
| `open`, `high`, `low`, `close` | OHLC for the minute |
| `volume` | Shares traded in this minute |
| `value_traded` | Rupee value of this minute's trades |
| `vwap_so_far` | Cumulative VWAP since market open (running) |
| `as_of_date` | Date this row was written |

Partitioned by `year/month/day`. ~750k rows/day compressed. After 1 year, ~5 GB. After 5 years, ~25 GB.

Used for: ORB level computation, VWAP entry timing, intraday F&O cross-checks, intraday backtests once enough data accumulates (usable after ~12 months of forward collection).

### `index_constituents_history` table (SQLite)

Critical for survivorship-bias correction in backtests. Captures which stocks were members of which indices on which dates.

| Field | Purpose |
|-------|---------|
| `index_name` | e.g., `NIFTY_50`, `NIFTY_500`, `NIFTY_BANK` |
| `stock_symbol`, `exchange` | Constituent |
| `effective_from` | Date stock was added to index |
| `effective_to` | Date stock was removed (NULL if still member) |
| `change_reason` | `addition`, `removal`, `delisting`, `merger`, `rename` |
| `source_announcement_url` | Link to NSE methodology / press release |

Backfilled once from NSE methodology archives (quarterly reconstitution announcements). Maintained quarterly thereafter.

Backtests query: `WHERE effective_from <= D AND (effective_to IS NULL OR effective_to > D)` to get the historical universe for any date D. This eliminates the silent ~3-5% annual return inflation from survivorship bias.

### `corporate_actions` table (SQLite)

| Field | Purpose |
|-------|---------|
| `action_id` | UUID |
| `raw_id`, `parser_version` | Provenance |
| `stock_symbol`, `exchange` | What |
| `action_type` | `split`, `bonus`, `dividend`, `rights`, `merger`, `delisting`, `name_change` |
| `announcement_date` | When announced |
| `record_date` | Record date for eligibility |
| `ex_date` | Ex-date in market |
| `observed_at` | When we observed the announcement |
| `ratio_or_amount` | e.g., "1:5" for 5-for-1 split; amount for dividend |
| `notes` | Free text from source |

Used for retroactive price adjustment in the parquet lake. Stored in operational SQLite (low volume, frequently joined).

### `quarterly_financials` table (SQLite)

Structured financial data scraped from Screener.in HTML tables. Required by the `earnings_quality` signal.

| Field | Purpose |
|-------|---------|
| `fin_id` | UUID |
| `stock_symbol`, `exchange` | Company |
| `period_end` | Quarter end date (e.g., 2024-03-31) |
| `period_type` | `Q` (quarterly) or `A` (annual) |
| `revenue` | Net sales / revenue (₹ crore) |
| `operating_profit` | EBITDA or EBIT as reported (₹ crore) |
| `opm_pct` | Operating profit margin % |
| `pat` | Profit after tax (₹ crore) |
| `cfo` | Cash from operations (₹ crore) |
| `source_url` | Screener.in URL scraped |
| `scraped_at` | When this was fetched |
| `observed_at` | Set to `results_announcement_date + 2h` |

Rate: 1 request per 5 seconds, 2–6 AM IST only. Trigger: scrape within 48 hours of quarterly results filing. Stored in SQLite (~50 MB after 5 years).

### `instruments` table (SQLite)

Cross-broker, cross-exchange instrument master. Resolves identifier fragmentation: BSE uses scrip codes, NSE uses symbols, Kite uses numeric tokens, Fyers uses `NSE:SYMBOL-EQ` format.

| Field | Purpose |
|-------|---------|
| `isin` | ISIN — universal identifier |
| `nse_symbol` | NSE trading symbol |
| `bse_code` | BSE scrip code |
| `company_name` | Canonical name |
| `kite_instrument_token` | Kite numeric token |
| `kite_tradingsymbol` | Kite trading symbol |
| `fyers_symbol` | Fyers format: `NSE:SYMBOL-EQ` |
| `series` | `EQ`, `BE`, etc. |
| `face_value` | Per share |
| `last_refreshed` | When this row was last updated |

All collector joins from BSE data → NSE prices → broker tokens go through this table.

## Filing sentiment — FinBERT

Filing sentiment (`sentiment_label`, `sentiment_confidence` in the `filings` table) is computed using **FinBERT running locally**. No external API calls.

**Model:** `ProsusAI/finbert` — pre-trained on financial text. Produces three-class output: `positive`, `negative`, `neutral`.

**Local deployment:** Model weights (~440 MB) stored at `/opt/boomer/models/finbert/`. Loaded at startup of the parse worker. CPU inference ~80–120ms per filing.

**Inference pipeline:**
```
Input:  filing.headline + " " + filing.body_summary[:500]
Model:  ProsusAI/finbert via HuggingFace transformers pipeline("text-classification")
Output: {"label": "positive"|"negative"|"neutral", "score": 0.0–1.0}
```

Confidence threshold: `sentiment_confidence < 0.60` → stored as `unclassified`. Threshold is configurable via `risk_config.sentiment_confidence_threshold`.

Runs **during parse phase** (Layer 1 → Layer 2), batch of up to 32 filings per inference call.

## Rate limiting and politeness

### Principles

1. **Respect Crawl-Delay.** If robots.txt says 5 seconds, do 5 seconds.
2. **Random jitter.** Not exactly 5.0s — between 4.5s and 6.5s.
3. **Exponential backoff on errors.** First failure: 30s. Second: 1 min. Third: 5 min. Fourth: 30 min. Fifth: alert and pause.
4. **Single-threaded per host.** No parallel requests to the same domain.
5. **User-Agent rotation.** Small pool of realistic browser UAs, rotated per session.
6. **Cookie management.** NSE requires hitting the homepage first to get session cookies.
7. **Off-peak when possible.** EOD data fetched at midnight, not 6 PM peak.

### Rate limit budget

- BSE filings: 1 request per 60 seconds during business hours
- NSE filings: 1 request per 90 seconds
- Bulk deals: 2 requests per day total
- Prices (NSE bhavcopy): 1 request per day (single file download)
- Index data: 1 request per 5 minutes
- Screener.in quarterly financials: 1 request per 5 seconds, 2–6 AM IST only
- NSE CM Bhavcopy with Market Cap: 1 request per day

Total request rate: well under 1/second average. **Will not get blocked at this rate.**

**No rotating proxies for v1.**

## Failure isolation

Each source's collection runs in its own try/except block. Failures logged to `collection_runs` table; orchestrator continues to next source.

| Field | Purpose |
|-------|---------|
| `run_id` | UUID |
| `source` | Which source |
| `started_at`, `ended_at` | Timing |
| `status` | `success`, `partial`, `failed`, `skipped` |
| `records_fetched` | Count |
| `records_new` | After dedup |
| `error_message` | If failed |
| `retry_count` | Attempts made |

## Data freshness SLAs

| Source | Maximum acceptable staleness |
|--------|------------------------------|
| Bulk deals (yesterday's) | By 8 AM next day |
| Daily prices | By 7 AM next day |
| BSE/NSE filings | Within 2 hours of publication |
| Promoter changes | Within 4 hours |
| Index data | Within 1 hour |
| F&O OI daily | By 8 AM next day |
| Stock master | 7 days |

If data is staler than SLA, freshness flag flips. Signals dependent on stale data won't fire that day. **Stale data leading to wrong decisions is worse than no decisions.**

## Daily schedule

| Time (IST) | What runs |
|------------|-----------|
| 02:00 | Off-peak EOD fetch — yesterday's bulk deals, prices, F&O OI |
| 06:30 | Pre-market verification — confirm yesterday's data complete |
| 07:00 | Hand-off to System 2 (analyser starts) |
| 09:30 — 15:30 | Intraday polling every 30 min (filings, promoter changes) + WebSocket minute bars |
| 16:00 | Market close fetch — today's prices, index closes |
| 18:00 | Evening filings sweep — final 2-hour window |
| 20:00 | Daily reconciliation — match fetch counts vs expected |

## Freshness graph (dependency tracking)

Some data depends on other data. Example: "promoter holding change %" needs both previous holding (prior filing) and new holding (current filing). If the prior filing isn't parsed yet, the change is meaningless.

**Decision:** maintain a dependency graph at parse time. Each Layer 2 row knows what other rows it depends on. If a dependency is missing or stale, the row is marked `pending_dependencies`. When the dependency arrives, the dependent row is re-parsed automatically.

## Loopholes and decisions

### Loophole 1: Corrections aren't always announced

NSE silently updates bulk deal data sometimes — same URL, different content. Hash check catches the change but doesn't say which records changed.

**Decision:** When `content_hash` changes for an already-archived URL, treat the new payload as authoritative, parse it, and mark previously-parsed records from the old payload as `corrects_*`. Original raw is preserved (different `raw_id`).

### Loophole 2: Symbol changes break linkage

Companies rename. Symbols change.

**Decision:** Maintain `symbol_history` table mapping `(old_symbol, new_symbol, effective_date)`. All queries join through this when needed. Built lazily as symbols change.

### Loophole 3: Time zones

NSE publishes IST. Servers might be UTC. Broker APIs may differ.

**Decision:** Store all timestamps as UTC. Convert at display time only. Trading day boundary = "Indian trading day" computed from UTC + IST offset, with 9:15 AM IST market open as the day boundary.

### Loophole 4: "First time we see X" for backfill

For point-in-time correctness, `observed_at` matters. But initial backfill data gets the same `observed_at` even though events span years.

**Decision:** For initial backfill, set `observed_at = filing_date + 1 hour`. Document this as a known imperfection of pre-system-start data. Backtests treat pre-system-start data with explicit awareness.

### Loophole 5: Storage growth

Raw archive grows ~5-10 GB/year. After 5 years, ~50 GB.

**Decision:** Gzip everything in raw archive (5-10x compression for HTML/JSON). After 1 year, move to colder storage (separate disk or local archive). Layer 2 stays hot. Re-parse from cold storage is rare.

### Loophole 6: Authoritative source for prices (revised)

Two candidates: scraped NSE/BSE bhavcopy vs Kite Connect's API.

**Decision (revised):** NSE bhavcopy is primary for historical and EOD data — authoritative, free, complete. Broker API (Kite) is used for *live tick data* during market hours and for cross-checking same-day EOD against bhavcopy. If broker and bhavcopy disagree by > 0.1% on close prices, alert. Yahoo Finance via `yfinance` provides a third independent source for weekly verification.

This inverts the original design (which had broker primary). Reasoning: broker data is itself derived from NSE, so going direct removes a layer of potential drift. Also avoids paying for historical data that NSE publishes free.

### Loophole 7: Parser version migration

When parser v2 ships, do we re-parse all historical data?

**Decision:** Yes, but in the background. Spawn a background job that re-parses raw archive with the new parser. Mark new rows with `parser_version=v2`. Downstream queries get latest version by default.

### Loophole 8: F&O OI contract gap (post-review fix)

The original design listed only "F&O lot sizes" (Category C, monthly static) as the F&O data source. But Phase 3 specifies F&O signals weighted at 0.20 in the intraday track requiring overnight OI build-up, max pain proximity, and IV percentile — all of which need *daily* OI data. The contract was broken: brain assumed data the collector never produced.

**Decision:** Added `fo_oi_daily` as a Category A daily source with full schema, parsed from NSE F&O bhavcopy. Added `prices_minute` as a Category D forward-streaming source for intraday VWAP/ORB features. Added derived feature definitions (`overnight_oi_change_pct`, `max_pain_strike`, `iv_percentile_252d`, `put_call_ratio_*`) so the contract between Phase 2 and Phase 3 is now explicit.

### Loophole 9: Survivorship bias in historical universe (post-review fix)

If backtests use the *current* Nifty 500 for historical periods, results inflate by 3-5% annually because delisted/failed companies aren't in the test set.

**Decision:** Added `index_constituents_history` table backfilled from NSE methodology archives. Backtests query historical universe per-date instead of using current membership. This eliminates one of the largest sources of backtest bias.

## What this design buys

1. **Audit trail to the byte.** Every trade decision can be traced back to original scraped HTML/JSON.
2. **Replayable history.** Improve a parser, re-run on archive, get better historical data without re-fetching.
3. **Honest about what we don't know.** Freshness checks ensure stale data doesn't silently feed signals.
4. **Survives source changes.** When NSE redesigns their API, only one fetcher breaks. The rest keep running.
5. **ML-ready from day one.** Every record has provenance, version, observed_at.
6. **Backtester reads scale to decades of data.** Operational SQLite stays small and fast. Parquet lake handles 100+ GB with sub-second queries via DuckDB.
7. **Forward-collection means data accumulates without effort.** Every trading day adds one day of resolution to the historical store.
8. **No paid historical data dependency.** Free-source layered strategy (NSE bhavcopy primary, Yahoo verification, Screener fundamentals) covers all needs at zero recurring cost.

## Stop conditions for Phase 2 (all met)

- Storage architecture: two-layer (raw + parsed) plus operational/historical split locked
- Operational SQLite vs historical parquet lake decision locked
- DuckDB chosen as parquet query engine
- Source taxonomy (A/B/C/D categories) defined including live streaming
- Fetcher anatomy (5-method standard) specified
- Layer 1 schema (raw archive) defined
- Layer 2 schemas: filings, bulk_deals, promoter_changes, shares_outstanding, prices (SQLite window), fo_oi_daily, prices_minute (parquet), index_constituents_history, corporate_actions, quarterly_financials, instruments
- Free-source layered strategy: NSE bhavcopy primary, Yahoo verification, Screener fundamentals, Kite for live only
- Forward-collection principle locked: minute bars accumulate from day one via parquet
- Rate limit budgets per source set
- Failure isolation pattern specified
- Freshness SLAs defined (including F&O OI)
- Daily schedule locked
- 9 loopholes identified with decisions (7 original + 2 post-review)
