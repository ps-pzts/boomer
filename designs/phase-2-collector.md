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

## Source taxonomy

Sources have very different behaviours; mixing them up causes bugs.

### Category A — Daily snapshot sources

Provide "the state as of end of day." Polled once daily after market close.

- Bulk deals (BSE + NSE) — published EOD around 6 PM
- Block deals (BSE + NSE) — published EOD around 6 PM
- Daily OHLCV (broker API) — official close around 4 PM
- Index values — same
- Trading calendar — yearly, weekly verification
- **F&O OI data (NSE FO bhavcopy)** — daily Open Interest and OI change per instrument, published EOD around 6 PM. Required by the intraday track's F&O signals (overnight OI build-up, max pain proximity).
- **NSE CM Bhavcopy with Market Cap** — daily CSV published by NSE including TOTAL_SHARES (total issued capital per company). Required to compute promoter holding percentages from SAST filings. URL: NSE's CM archives, "BhavCopy with Market Cap" variant. This is distinct from the standard CM bhavcopy.

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
- **Quarterly financial results (Screener.in HTML)** — fetched per company after each quarterly results announcement; see `quarterly_financials` schema below. Rate: 1 request per 2 seconds, off-peak hours only.
- **Instrument master (Kite + Fyers instrument CSV)** — weekly refresh; maps instrument tokens, ISINs, symbols across exchanges and brokers; see `instruments` table below.

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

### `filings` table

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
| `is_corrected` | Boolean |
| `corrects_filing_id` | If corrected, which filing it replaces |

The `observed_at` is the magical field. Backtest queries use `WHERE observed_at <= simulation_date`.

### `bulk_deals` table

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

### `promoter_changes` table

Similar pattern with `previous_holding_pct`, `new_holding_pct`, `transaction_mode` (`open_market`, `preferential`, `pledged`, `released_pledge`), and `event_date` separate from `observed_at`.

**Promoter percentage calculation:** SAST Regulation 31 filings disclose the raw number of shares held by promoters. To compute `holding_pct`, the system needs total shares outstanding. Formula:

```
promoter_holding_pct = (sum of promoter shares from SAST) / total_shares_outstanding
```

`total_shares_outstanding` comes from the NSE CM Bhavcopy with Market Cap (TOTAL_SHARES column), joined on ISIN. This is computed at feature time in Stage 0, not stored in `promoter_changes` itself. The `promoter_changes` table stores raw share counts from SAST filings; the percentage is a derived feature.

### `fo_oi_data` table

Daily Open Interest snapshot required by the intraday signal track.

| Field | Purpose |
|-------|---------|
| `oi_id` | UUID |
| `raw_id`, `parser_version` | Provenance |
| `stock_symbol`, `exchange` | Underlying |
| `instrument_type` | `CE`, `PE`, `FUT` |
| `expiry_date` | Contract expiry |
| `strike_price` | For options; NULL for futures |
| `trade_date` | The trading day this snapshot covers |
| `observed_at` | When we first saw it |
| `open_interest` | Total OI in contracts |
| `oi_change` | Change from previous session |
| `volume` | Total contracts traded |
| `settle_price` | Settlement price |
| `is_corrected`, `corrects_oi_id` | Correction pattern |

**Derived fields computed at feature time (not stored raw):**

- `max_pain_price` — strike with maximum combined OI loss for option writers; computed per expiry per day
- `pcr` — Put-Call Ratio (total put OI / total call OI) per stock per expiry

These are features, not raw data — computed in Stage 0 from this table.

**Source:** NSE FO bhavcopy (publicly available daily CSV). Fallback: Kite Connect's historical OI endpoint for recent dates.

### `prices` table

| Field | Purpose |
|-------|---------|
| `stock_symbol`, `exchange`, `trade_date` | Identity |
| `open`, `high`, `low`, `close` | OHLC |
| `volume` | Total traded shares |
| `value_traded` | Total ₹ |
| `is_adjusted` | Boolean — adjusted for corporate actions? |
| `adjustment_factor` | If adjusted, cumulative factor |
| `as_of_date` | When this row's adjustment was last computed |

The price table has a special wrinkle: corporate actions retroactively change historical prices. Both raw and adjusted are stored, plus adjustment metadata, so backtests can use unadjusted prices for "what we knew at the time."

### `shares_outstanding` table

Daily total issued capital per company, derived from the NSE CM Bhavcopy with Market Cap file.

| Field | Purpose |
|-------|---------|
| `isin` | ISIN (primary key with date) |
| `stock_symbol`, `exchange` | NSE symbol |
| `trade_date` | Date this count applies to |
| `total_shares` | Total issued capital (from NSE bhavcopy TOTAL_SHARES column) |
| `observed_at` | When we fetched this |

Changes when bonuses, splits, or rights issues occur — hence daily rather than static.

### `quarterly_financials` table

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
| `cfo` | Cash from operations (₹ crore) — from cash flow table |
| `source_url` | Screener.in URL scraped |
| `scraped_at` | When this was fetched |
| `observed_at` | Set to `results_announcement_date + 2 hours` for point-in-time correctness |

**Scraping approach:** `pd.read_html(screener_url)` parses the Quarterly Results and Cash Flow tables. The URL pattern is `https://www.screener.in/company/{NSE_SYMBOL}/`. Fall back to `BeautifulSoup` if `pd.read_html` cannot identify the correct table (Screener.in structure is stable but verify on deployment). Rate: 1 request per 2 seconds, between 2 AM–6 AM IST only. Trigger: scrape within 48 hours of a quarterly results filing appearing in the `filings` table with `category = quarterly_results`.

**Screener.in ToS note:** Screener.in permits personal use but prohibits commercial redistribution. This usage (personal algo trading research) falls within personal use. Verify this interpretation against their current ToS before deployment.

### `instruments` table

Cross-broker, cross-exchange instrument master. Resolves the identifier fragmentation problem: BSE uses scrip codes, NSE uses symbols, Kite uses numeric instrument tokens, Fyers uses `NSE:SYMBOL-EQ` format.

| Field | Purpose |
|-------|---------|
| `isin` | ISIN — the universal identifier |
| `nse_symbol` | NSE trading symbol |
| `bse_code` | BSE scrip code |
| `company_name` | Canonical name |
| `kite_instrument_token` | Kite numeric token (from Kite instruments CSV) |
| `kite_tradingsymbol` | Kite trading symbol (usually matches NSE) |
| `fyers_symbol` | Fyers format: `NSE:SYMBOL-EQ` |
| `series` | `EQ` (equity), `BE` (book entry), etc. |
| `face_value` | Per share |
| `last_refreshed` | When this row was last updated |

**Population:** Kite publishes a daily instruments CSV at `https://api.kite.trade/instruments`. This CSV includes ISIN, instrument_token, tradingsymbol for all tradeable instruments. NSE securities master (downloadable from NSE) provides the BSE-NSE cross-reference via ISIN. Weekly refresh. All collector joins go through this table — BSE filing data is joined to NSE price data via ISIN → nse_symbol.

**Why this matters:** A BSE filing for "Reliance Industries Ltd" with BSE scrip code 500325 must be joined to NSE price data for "RELIANCE" and Kite token 738561. Without the instruments table, this join is either name-matching (fragile) or manual. The instruments table makes it deterministic.

## Rate limiting and politeness

NSE will block aggressive scraping. BSE has been more lenient but is tightening.

### Principles

1. **Respect Crawl-Delay.** If robots.txt says 5 seconds, do 5 seconds.
2. **Random jitter.** Not exactly 5.0s — between 4.5s and 6.5s.
3. **Exponential backoff on errors.** First failure: 30s. Second: 1 min. Third: 5 min. Fourth: 30 min. Fifth: alert and pause.
4. **Single-threaded per host.** No parallel requests to the same domain.
5. **User-Agent rotation.** Small pool of realistic browser UAs, rotated per session.
6. **Cookie management.** NSE specifically requires hitting the homepage first to get session cookies before API endpoints work.
7. **Off-peak when possible.** EOD data fetched at midnight, not 6 PM peak.

### Rate limit budget

- BSE filings: 1 request per 60 seconds during business hours
- NSE filings: 1 request per 90 seconds
- Bulk deals: 2 requests per day total
- Prices: from broker API (Kite ~3 req/s)
- Index data: 1 request per 5 minutes
- Screener.in quarterly financials: 1 request per 2 seconds, off-peak only (2–6 AM)
- NSE CM Bhavcopy with Market Cap: 1 request per day (single file download)

## Filing sentiment — FinBERT

Filing sentiment (`sentiment_label`, `sentiment_confidence` in the `filings` table) is computed using **FinBERT running locally**. No external API calls.

### Model choice

**Model:** `ProsusAI/finbert` — pre-trained on financial text (financial news, earnings call transcripts, analyst reports). Produces three-class output: `positive`, `negative`, `neutral` with softmax probabilities.

**Why FinBERT over general BERT:** Financial language has domain-specific meaning. "Challenging environment," "headwinds," and "disappointing results" are reliably negative in financial context in ways a general-purpose model might miss. FinBERT was trained on this vocabulary.

**Local deployment:** Model weights (~440 MB) stored at `/opt/boomer/models/finbert/`. Loaded at startup of the parse worker. No GPU required — CPU inference takes ~80–120ms per filing on a modern VPS CPU, which is acceptable for batch processing.

### Inference pipeline

```
Input:  filing.headline + " " + filing.body_summary[:500]
        (combined into a single text, max ~600 chars, within FinBERT's 512-token limit)

Model:  ProsusAI/finbert via HuggingFace transformers pipeline("text-classification")

Output: {"label": "positive"|"negative"|"neutral", "score": 0.0–1.0}

Stored: sentiment_label = label
        sentiment_confidence = score
```

### When inference runs

Sentiment is computed **during the parse phase** (Layer 1 → Layer 2), not inline during collection. The parse step calls FinBERT for each new filing row. Batch inference: process up to 32 filings at once per inference call for efficiency.

### Confidence threshold

Filings with `sentiment_confidence < 0.60` are stored with `sentiment_label = "unclassified"` rather than a low-confidence label. The filing_score feature in Stage 3 treats `unclassified` as neutral (0.0 contribution). This threshold is configurable in `risk_config`.

### Reliability and known limitations

- FinBERT was trained on English financial text. BSE/NSE filings are in English but use Indian corporate language patterns. Accuracy on Indian filings is expected to be 75–85% vs the 90%+ reported on the training dataset.
- Headline-only inference is less accurate than full-document inference. Using headline + body summary (first 500 chars) is a deliberate tradeoff between accuracy and latency.
- Model is versioned: `finbert_version` stored alongside `parser_version` in the `filings` table. Re-running sentiment on historical filings with a newer model is a standard re-parse operation.

Total request rate: well under 1/second average. **Will not get blocked at this rate.**

### Proxy decision

**No rotating proxies for v1.** Cost money, add failure modes, signal evasion. The scraping pattern is gentle enough to look like a personal tracker.

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

This becomes the dashboard's "data health" panel. One source down ≠ system down.

## Data freshness SLAs

| Source | Maximum acceptable staleness |
|--------|------------------------------|
| Bulk deals (yesterday's) | By 8 AM next day |
| Daily prices | By 7 AM next day |
| BSE/NSE filings | Within 2 hours of publication |
| Promoter changes | Within 4 hours |
| Index data | Within 1 hour |
| F&O OI data (yesterday's) | By 8 AM next day |
| Stock master | 7 days |

If F&O OI data is stale, intraday signals that rely on F&O inputs (`pre_market_gap`, `f_and_o_signals`) will have their `data_freshness_factor` set to 0.0, which suppresses those sub-signals entirely (see freshness contract in Loopholes section).

If data is staler than SLA, freshness flag flips. Signals dependent on stale data won't fire that day. **Stale data leading to wrong decisions is worse than no decisions.**

## Daily schedule

| Time (IST) | What runs |
|------------|-----------|
| 02:00 | Off-peak EOD fetch — yesterday's bulk deals, prices |
| 06:30 | Pre-market verification — confirm yesterday's data complete |
| 07:00 | Hand-off to System 2 (analyser starts) |
| 09:30 — 15:30 | Intraday polling every 30 min |
| 16:00 | Market close fetch — today's prices, index closes |
| 18:00 | Evening filings sweep — final 2-hour window |
| 20:00 | Daily reconciliation — match fetch counts vs expected |

The 07:00 hand-off is the original requirement. By that time, the collector has already done EOD work overnight, so System 2 has fresh data.

## Freshness graph (dependency tracking)

Some data depends on other data. Example: "promoter holding change %" needs both previous holding (prior filing) and new holding (current filing). If the prior filing isn't parsed yet, the change is meaningless.

**Decision:** maintain a dependency graph at parse time. Each Layer 2 row knows what other rows it depends on. If a dependency is missing or stale, the row is marked `pending_dependencies`. When the dependency arrives, the dependent row is re-parsed automatically.

One extra column, one extra check. Pays for itself the first time a parser bug needs cascading re-parse.

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

### Loophole 6: Broker API for prices vs scraped prices

Zerodha gives OHLCV via Kite Connect. Should we trust it as authoritative or also scrape NSE bhavcopy as backup?

**Decision:** Broker API is primary, NSE bhavcopy is daily reconciliation check. If they disagree by more than 0.1%, alert. Different sources occasionally have minor adjustment differences; large divergences are bugs.

### Loophole 7: Parser version migration

When parser v2 ships, do we re-parse all historical data?

**Decision:** Yes, but in the background. Spawn a background job that re-parses raw archive with the new parser. Mark new rows with `parser_version=v2`. Downstream queries get latest version by default.

### Loophole 8: Data freshness — binary suppression vs. confidence degradation

Phase 3's confidence formula uses `data_freshness_factor = exp(-days_since_max_observed / characteristic_decay)` which gracefully degrades confidence as data ages. But Phase 2 also states "stale data leading to wrong decisions is worse than no decisions." These are two different behaviors: gradual degradation vs. hard suppression.

**Decision:** Both apply, with an explicit threshold:

- **`data_freshness_factor ≥ 0.3`** — signal may fire with reduced confidence. The freshness factor multiplies into the confidence formula, naturally lowering the signal's weight in portfolio construction and EV gate.
- **`data_freshness_factor < 0.3`** — signal is **fully suppressed**. No recommendation generated for that sub-signal. The sub-signal contributes 0 to the composite signal score, as if the data source was absent.

The characteristic_decay value per source:

| Source | Characteristic decay (days) |
|--------|------------------------------|
| Bulk deals | 3 |
| Promoter changes | 7 |
| Filings | 1 (event-based; stale filing sentiment is quickly outdated) |
| Prices / technicals | 1 |
| F&O OI data | 1 |

At 0.3 threshold with these decay values: filing sentiment is suppressed after ~1.2 days, F&O OI after ~1.2 days, bulk deals after ~3.6 days. This is the "freshness SLA" translated into signal behavior.

### Loophole 9: F&O OI data not available for early backfill

NSE FO bhavcopy is available from 2003 onward as historical CSV files. For the walk-forward validation window (2020-2024), complete daily OI data should be obtainable. Initial backfill: download NSE FO bhavcopy archives by year and bulk-load into `fo_oi_data` table. Set `observed_at = trade_date + 18:30 IST` (approximate NSE publication time) for all historical rows, consistent with the Phase 2 backfill convention.

## What this design buys

1. **Audit trail to the byte.** Every trade decision can be traced back to original scraped HTML/JSON.

2. **Replayable history.** Improve a parser, re-run on archive, get better historical data without re-fetching.

3. **Honest about what we don't know.** Freshness checks ensure stale data doesn't silently feed signals.

4. **Survives source changes.** When NSE redesigns their API, only one fetcher breaks. The rest keep running.

5. **ML-ready from day one.** Every record has provenance, version, observed_at. In 18 months, training models on historically-accurate features without rebuilding the data layer.

## Stop conditions for Phase 2 (all met)

- Storage architecture (raw + parsed two-layer) locked
- Source taxonomy (A/B/C categories) defined; all sources accounted for
- Fetcher anatomy (5-method standard) specified
- Layer 1 schema (raw archive) defined
- Layer 2 schemas defined: filings, bulk_deals, promoter_changes, fo_oi_data, prices, shares_outstanding, quarterly_financials, instruments
- F&O OI data: Category A daily source, schema, SLA, backfill path
- Shares outstanding: NSE CM Bhavcopy with Market Cap (TOTAL_SHARES column), daily, joined via ISIN
- Promoter % calculation: SAST share count / TOTAL_SHARES from bhavcopy (computed at feature time)
- Quarterly financials: Screener.in HTML scraping via pd.read_html(), Category C per company post-results
- Instruments table: cross-broker/exchange ISIN master (Kite instruments CSV + NSE securities master)
- Filing sentiment: FinBERT (ProsusAI/finbert) running locally; batch inference at parse time; confidence < 0.60 → unclassified
- Rate limit budgets per source set (including Screener.in)
- Failure isolation pattern specified
- Freshness SLAs defined (including F&O OI)
- Freshness suppression contract: threshold < 0.3 → suppress; ≥ 0.3 → degrade confidence
- Daily schedule locked
- 9 loopholes identified with decisions
