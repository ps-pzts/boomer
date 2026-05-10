"""Feature computers — Stage 0.

Each function takes raw rows from the collector tables and writes features
to the FeatureStore. All features must be backfillable from raw_archive alone.

Naming convention: feature names match the keys expected by signal generators.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from brain.feature_store import FeatureStore


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def compute_promoter_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute promoter activity features for a stock on a given date.

    Requires: promoter_changes table and shares_outstanding table.
    Writes:
        promoter_holding_pct_change_90d
        promoter_open_market_buy_count_90d
        promoter_pledge_pct_current
    """
    cutoff = (as_of_date - timedelta(days=90)).isoformat()
    as_of_str = as_of_date.isoformat()

    with _conn(db_path) as conn:
        # Total shares for denominator
        so_row = conn.execute(
            """
            SELECT shares_outstanding FROM shares_outstanding
            WHERE symbol = ? AND exchange = ? AND observed_date <= ?
            ORDER BY observed_date DESC LIMIT 1
            """,
            (stock_symbol, exchange, as_of_str),
        ).fetchone()

        if so_row is None:
            return  # Cannot compute without shares_outstanding (design doc constraint)

        total_shares = float(so_row["shares_outstanding"])
        if total_shares <= 0:
            return

        # Promoter holding 90 days ago
        past_row = conn.execute(
            """
            SELECT SUM(acquirer_shares_after) AS holding
            FROM promoter_changes
            WHERE symbol = ? AND exchange = ?
              AND is_promoter = 1 AND observed_at <= ?
            ORDER BY observed_at DESC LIMIT 1
            """,
            (stock_symbol, exchange, cutoff),
        ).fetchone()

        # Promoter holding as of today
        current_row = conn.execute(
            """
            SELECT SUM(acquirer_shares_after) AS holding
            FROM promoter_changes
            WHERE symbol = ? AND exchange = ?
              AND is_promoter = 1 AND observed_at <= ?
            ORDER BY observed_at DESC LIMIT 1
            """,
            (stock_symbol, exchange, as_of_str),
        ).fetchone()

        past_holding = float(past_row["holding"] or 0) if past_row else 0.0
        current_holding = float(current_row["holding"] or 0) if current_row else 0.0

        holding_pct_past = (past_holding / total_shares) * 100.0
        holding_pct_now = (current_holding / total_shares) * 100.0
        holding_change = holding_pct_now - holding_pct_past

        # Open-market buy count over 90 days
        buy_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM promoter_changes
            WHERE symbol = ? AND exchange = ?
              AND is_promoter = 1 AND transaction_type = 'buy'
              AND transaction_mode = 'market' AND observed_at > ? AND observed_at <= ?
            """,
            (stock_symbol, exchange, cutoff, as_of_str),
        ).fetchone()
        buy_count = int(buy_row["cnt"] or 0) if buy_row else 0

        # Latest pledge percentage
        pledge_row = conn.execute(
            """
            SELECT promoter_pledge_pct FROM promoter_changes
            WHERE symbol = ? AND exchange = ? AND observed_at <= ?
              AND promoter_pledge_pct IS NOT NULL
            ORDER BY observed_at DESC LIMIT 1
            """,
            (stock_symbol, exchange, as_of_str),
        ).fetchone()
        pledge_pct = float(pledge_row["promoter_pledge_pct"]) if pledge_row else 0.0

    wf = fs.write_feature
    d, sym, exc = as_of_date, stock_symbol, exchange
    wf(sym, exc, "promoter_holding_pct_change_90d", holding_change, d, d)
    wf(sym, exc, "promoter_open_market_buy_count_90d", float(buy_count), d, d)
    wf(sym, exc, "promoter_pledge_pct_current", pledge_pct, d, d)


def compute_smart_money_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute smart money (bulk deals) features.

    Writes:
        smart_money_net_buy_value_90d
        smart_money_buyer_count_90d
    """
    cutoff = (as_of_date - timedelta(days=90)).isoformat()
    as_of_str = as_of_date.isoformat()

    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT is_buy, quantity, price, is_smart_money
            FROM bulk_deals
            WHERE symbol = ? AND exchange = ?
              AND is_smart_money = 1
              AND deal_date > ? AND deal_date <= ?
            """,
            (stock_symbol, exchange, cutoff, as_of_str),
        ).fetchall()

    net_value = 0.0
    buyers: set[str] = set()
    for row in rows:
        value = float(row["quantity"]) * float(row["price"])
        if row["is_buy"]:
            net_value += value
        else:
            net_value -= value

    wf = fs.write_feature
    d, sym, exc = as_of_date, stock_symbol, exchange
    wf(sym, exc, "smart_money_net_buy_value_90d", net_value, d, d)
    wf(sym, exc, "smart_money_buyer_count_90d", float(len(buyers)), d, d)


def compute_filing_sentiment_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
    sentiment_confidence_threshold: float = 0.60,
) -> None:
    """Compute filing sentiment and red-flag features.

    Writes:
        filing_bullish_count_90d
        filing_bearish_count_90d
        has_auditor_change_90d
        has_pledging_increase_90d
    """
    cutoff = (as_of_date - timedelta(days=90)).isoformat()
    as_of_str = as_of_date.isoformat()

    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT sentiment_label, sentiment_confidence, filing_category
            FROM filings
            WHERE symbol = ? AND exchange = ?
              AND observed_at > ? AND observed_at <= ?
            """,
            (stock_symbol, exchange, cutoff, as_of_str),
        ).fetchall()

    bullish = 0
    bearish = 0
    has_auditor_change = False
    has_pledging_increase = False

    for row in rows:
        conf = float(row["sentiment_confidence"] or 0)
        label = row["sentiment_label"]
        cat = row["filing_category"] or ""

        if conf >= sentiment_confidence_threshold:
            if label == "positive":
                bullish += 1
            elif label == "negative":
                bearish += 1

        if "auditor" in cat.lower():
            has_auditor_change = True
        if "pledge" in cat.lower() and "increase" in cat.lower():
            has_pledging_increase = True

    wf = fs.write_feature
    d, sym, exc = as_of_date, stock_symbol, exchange
    wf(sym, exc, "filing_bullish_count_90d", float(bullish), d, d)
    wf(sym, exc, "filing_bearish_count_90d", float(bearish), d, d)
    wf(sym, exc, "has_auditor_change_90d", 1.0 if has_auditor_change else 0.0, d, d)
    wf(sym, exc, "has_pledging_increase_90d", 1.0 if has_pledging_increase else 0.0, d, d)


def compute_earnings_quality_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute earnings quality features from Screener quarterly_financials.

    Returns without writing if fewer than 2 quarters are available.
    Writes:
        revenue_growth_yoy_pct
        opm_trend_4q
        cfo_pat_ratio_latest
    """
    as_of_str = as_of_date.isoformat()

    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT quarter_end_date, revenue, opm_pct, cfo, pat, observed_at
            FROM quarterly_financials
            WHERE symbol = ? AND exchange = ?
              AND observed_at <= ?
            ORDER BY quarter_end_date DESC
            LIMIT 8
            """,
            (stock_symbol, exchange, as_of_str),
        ).fetchall()

    if len(rows) < 2:
        return

    source_max = max(date.fromisoformat(r["observed_at"][:10]) for r in rows)
    wf = fs.write_feature
    sym, exc, d = stock_symbol, exchange, as_of_date

    # YoY revenue growth: compare Q0 vs Q4
    if len(rows) >= 5:
        rev_now = float(rows[0]["revenue"] or 0)
        rev_year_ago = float(rows[4]["revenue"] or 0)
        rev_growth = ((rev_now - rev_year_ago) / rev_year_ago * 100.0) if rev_year_ago else 0.0
        wf(sym, exc, "revenue_growth_yoy_pct", rev_growth, d, source_max)

    # OPM trend over 4 quarters (linear slope, normalised)
    opm_vals = [float(r["opm_pct"] or 0) for r in rows[:4]]
    if len(opm_vals) >= 4:
        # Simple linear trend: slope = (last - first) / 3
        slope = (opm_vals[0] - opm_vals[-1]) / 3.0
        wf(sym, exc, "opm_trend_4q", slope, d, source_max)

    # CFO/PAT ratio for most recent quarter
    latest = rows[0]
    cfo = float(latest["cfo"] or 0)
    pat = float(latest["pat"] or 0)
    if pat != 0:
        wf(sym, exc, "cfo_pat_ratio_latest", cfo / pat, d, source_max)


def compute_price_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute price-derived features: liquidity, ATR, volume z-score.

    Writes:
        avg_traded_value_20d
        atr_14d
        volume_zscore_5d
        pe_percentile_5y  (requires pe_ratio in prices table)
    """
    as_of_str = as_of_date.isoformat()
    cutoff_20 = (as_of_date - timedelta(days=30)).isoformat()

    with _conn(db_path) as conn:
        rows_20 = conn.execute(
            """
            SELECT close, high, low, volume, observed_date
            FROM prices
            WHERE symbol = ? AND exchange = ?
              AND observed_date > ? AND observed_date <= ?
            ORDER BY observed_date DESC
            LIMIT 20
            """,
            (stock_symbol, exchange, cutoff_20, as_of_str),
        ).fetchall()

    if not rows_20:
        return

    import statistics

    wf = fs.write_feature
    sym, exc, d = stock_symbol, exchange, as_of_date

    closes = [float(r["close"]) for r in rows_20]
    volumes = [float(r["volume"]) for r in rows_20]
    highs = [float(r["high"]) for r in rows_20]
    lows = [float(r["low"]) for r in rows_20]

    avg_value = sum(c * v for c, v in zip(closes, volumes, strict=True)) / len(closes)
    wf(sym, exc, "avg_traded_value_20d", avg_value, d, d)

    # ATR-14
    if len(rows_20) >= 14:
        atr14 = sum(highs[i] - lows[i] for i in range(14)) / 14.0
        wf(sym, exc, "atr_14d", atr14, d, d)

    # Volume z-score: (avg_5d - avg_20d) / std_20d
    if len(volumes) >= 20:
        avg5 = sum(volumes[:5]) / 5.0
        avg20 = sum(volumes) / 20.0
        std20 = statistics.stdev(volumes)
        if std20 > 0:
            wf(sym, exc, "volume_zscore_5d", (avg5 - avg20) / std20, d, d)
