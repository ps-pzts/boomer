"""Market-derived feature computers — Stage 0.

Each function computes features that require cross-stock data, F&O snapshots,
or market-context inputs beyond simple per-stock price history.

All functions share the same signature as computers.py:
    (db_path, fs, stock_symbol, exchange, as_of_date) -> None

Naming convention: feature keys match exactly what signal generators expect.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, timedelta

from brain.feature_store import FeatureStore


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ── Technical / chart structure ───────────────────────────────────────────────

def compute_technical_pattern_score(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute a price-structure proxy for the swing technical_setup sub-signal (weight 0.25).

    Three components (all in [-1, +1]):
      1. Price vs DMA-20 (weight 0.50) — trend confirmation.
         Normalised: price 3% above DMA-20 → +1.0; 3% below → -1.0.
      2. DMA-20 vs DMA-50 slope (weight 0.30) — momentum confirmation.
         Normalised: DMA-20 2% above DMA-50 → +1.0.  Skipped if DMA-50 absent.
      3. ATR contraction (weight 0.20) — base building / low-volatility setup.
         ATR < 1% of price → +1.0 (tight base); ATR > 3% of price → -1.0 (choppy).

    Must run AFTER compute_price_features in the registry (reads dma_20, dma_50,
    price_close, atr_14d from the FeatureStore).

    Writes:
        technical_pattern_score — [-1, +1]; positive = bullish structure
    """
    dma_20 = fs.get_feature_as_of(stock_symbol, exchange, "dma_20", as_of_date)
    price = fs.get_feature_as_of(stock_symbol, exchange, "price_close", as_of_date)

    if price is None or dma_20 is None or dma_20 == 0:
        return

    score = 0.0

    pct_vs_dma20 = (price - dma_20) / dma_20
    score += 0.50 * max(-1.0, min(1.0, pct_vs_dma20 / 0.03))

    dma_50 = fs.get_feature_as_of(stock_symbol, exchange, "dma_50", as_of_date)
    if dma_50 and dma_50 > 0:
        alignment = (dma_20 - dma_50) / dma_50
        score += 0.30 * max(-1.0, min(1.0, alignment / 0.02))

    atr = fs.get_feature_as_of(stock_symbol, exchange, "atr_14d", as_of_date)
    if atr is not None and price > 0:
        atr_pct = atr / price
        # Tight base (atr_pct < 0.01) → +1.0; choppy (atr_pct > 0.03) → -1.0
        contraction = (0.02 - atr_pct) / 0.02
        score += 0.20 * max(-1.0, min(1.0, contraction))

    fs.write_feature(
        stock_symbol, exchange, "technical_pattern_score",
        max(-1.0, min(1.0, score)), as_of_date, as_of_date,
    )


# ── F&O features ──────────────────────────────────────────────────────────────

def compute_fo_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute F&O-derived features from the fo_oi_daily table.

    Overnight OI change: percentage change in nearest-expiry futures open interest
    from the prior session.  Positive = new positions being built (bullish bias);
    negative = unwinding.

    Max pain: the strike price at which total option buyer losses are minimised
    (i.e. option writers win most).  Proximity = (close - max_pain) / close * 100.
    Negative proximity (price below max pain) is bearish — pinning risk.

    Requires price_close to have been written by compute_price_features first.

    Writes:
        fo_oi_overnight_change_pct — % OI change vs prior session (nearest-expiry futures)
        fo_max_pain_proximity_pct  — (close - max_pain_strike) / close * 100
    """
    as_of_str = as_of_date.isoformat()

    with _conn(db_path) as conn:
        fut_row = conn.execute(
            """
            SELECT open_interest, oi_change
            FROM fo_oi_daily
            WHERE underlying_symbol = ? AND trade_date = ? AND instrument_type = 'FUT'
            ORDER BY expiry_date ASC
            LIMIT 1
            """,
            (stock_symbol, as_of_str),
        ).fetchone()

        opt_rows = conn.execute(
            """
            SELECT instrument_type, strike_price, SUM(open_interest) AS total_oi
            FROM fo_oi_daily
            WHERE underlying_symbol = ? AND trade_date = ?
              AND instrument_type IN ('CE', 'PE') AND strike_price IS NOT NULL
            GROUP BY instrument_type, strike_price
            """,
            (stock_symbol, as_of_str),
        ).fetchall()

    wf = fs.write_feature
    sym, exc, d = stock_symbol, exchange, as_of_date

    if fut_row and fut_row["oi_change"] is not None:
        oi_now = float(fut_row["open_interest"])
        oi_delta = float(fut_row["oi_change"])
        prev_oi = oi_now - oi_delta
        if prev_oi > 0:
            wf(sym, exc, "fo_oi_overnight_change_pct", (oi_delta / prev_oi) * 100.0, d, d)

    if not opt_rows:
        return

    strikes: dict[float, dict[str, float]] = {}
    for r in opt_rows:
        s = float(r["strike_price"])
        if s not in strikes:
            strikes[s] = {"CE": 0.0, "PE": 0.0}
        strikes[s][r["instrument_type"]] = float(r["total_oi"])

    min_pain: float | None = None
    max_pain_strike: float | None = None
    for test_s in sorted(strikes):
        pain = sum(
            v["CE"] * max(0.0, test_s - s) + v["PE"] * max(0.0, s - test_s)
            for s, v in strikes.items()
        )
        if min_pain is None or pain < min_pain:
            min_pain = pain
            max_pain_strike = test_s

    current_close = fs.get_feature_as_of(sym, exc, "price_close", d)
    if max_pain_strike is not None and current_close and current_close > 0:
        proximity = (current_close - max_pain_strike) / current_close * 100.0
        wf(sym, exc, "fo_max_pain_proximity_pct", proximity, d, d)


# ── Cross-stock / sector features ─────────────────────────────────────────────

def compute_sector_relative_strength(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute the stock's 20-day return z-score within its sector peer group.

    sector_relative_strength_20d = (stock_20d_return - sector_mean) / sector_std

    Requires sector_classifications to be populated and at least 2 sector peers
    with 20 days of price history.  Skips silently if either condition is not met.

    Writes:
        sector_relative_strength_20d — z-score clipped to [-3, +3];
                                        positive = outperforming sector peers
    """
    import statistics

    as_of_str = as_of_date.isoformat()
    cutoff = (as_of_date - timedelta(days=30)).isoformat()

    with _conn(db_path) as conn:
        sector_row = conn.execute(
            """
            SELECT sector FROM sector_classifications
            WHERE symbol = ? AND exchange = ? AND effective_from <= ?
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            (stock_symbol, exchange, as_of_str),
        ).fetchone()

        if sector_row is None:
            return

        sector = sector_row["sector"]

        peer_rows = conn.execute(
            """
            SELECT DISTINCT symbol FROM sector_classifications
            WHERE sector = ? AND exchange = ? AND effective_from <= ?
            """,
            (sector, exchange, as_of_str),
        ).fetchall()

        peers = [r["symbol"] for r in peer_rows]

        def _ret20(sym: str) -> float | None:
            rows = conn.execute(
                """
                SELECT close FROM prices
                WHERE stock_symbol = ? AND exchange = ?
                  AND trade_date > ? AND trade_date <= ?
                ORDER BY trade_date DESC
                LIMIT 20
                """,
                (sym, exchange, cutoff, as_of_str),
            ).fetchall()
            if len(rows) < 2:
                return None
            first = float(rows[-1]["close"])
            return (float(rows[0]["close"]) - first) / first if first > 0 else None

        stock_ret = _ret20(stock_symbol)
        if stock_ret is None:
            return

        peer_returns = [
            r for sym in peers
            if sym != stock_symbol and (r := _ret20(sym)) is not None
        ]

    if len(peer_returns) < 2:
        return

    sector_mean = sum(peer_returns) / len(peer_returns)
    sector_std = statistics.stdev(peer_returns)

    if sector_std < 0.001:
        # Peers all moved identically; directional signal is still valid
        diff = stock_ret - sector_mean
        if abs(diff) < 1e-8:
            return
        rs = 3.0 if diff > 0 else -3.0
    else:
        rs = (stock_ret - sector_mean) / sector_std
    fs.write_feature(
        stock_symbol, exchange, "sector_relative_strength_20d",
        max(-3.0, min(3.0, rs)), as_of_date, as_of_date,
    )


# ── Momentum / mean-reversion classifier ─────────────────────────────────────

def compute_price_mode_classifier(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Classify price regime as momentum (+1) or mean-reversion (-1).

    Uses the lag-1 autocorrelation of the last 10 daily returns.
    Positive autocorrelation (returns cluster in the same direction) = momentum;
    negative autocorrelation (returns alternate) = mean reversion.

    Worked example (10 returns all positive → autocorr ≈ 1.0):
        rets = [+0.01, +0.01, ..., +0.01] (10 values)
        All deviations from mean are zero → cov = 0, denom = 0 → early return (flat market)
    A realistic trending market shows autocorr in [0.1, 0.5].

    Requires 12 rows of price data (11 returns + 1 lag).

    Writes:
        price_mode_classifier — [-1, +1]; +1 = momentum, -1 = mean reversion
    """
    as_of_str = as_of_date.isoformat()
    cutoff = (as_of_date - timedelta(days=20)).isoformat()

    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT close FROM prices
            WHERE stock_symbol = ? AND exchange = ?
              AND trade_date > ? AND trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT 12
            """,
            (stock_symbol, exchange, cutoff, as_of_str),
        ).fetchall()

    if len(rows) < 12:
        return

    closes = [float(r["close"]) for r in rows]
    rets = [(closes[i] - closes[i + 1]) / closes[i + 1] for i in range(11)]

    # Lag-1 autocorrelation between rets[0:10] and rets[1:11]
    r_t, r_t1 = rets[:10], rets[1:11]
    n = 10
    mean_t = sum(r_t) / n
    mean_t1 = sum(r_t1) / n

    cov = sum((r_t[i] - mean_t) * (r_t1[i] - mean_t1) for i in range(n)) / n
    var_t = sum((x - mean_t) ** 2 for x in r_t) / n
    var_t1 = sum((x - mean_t1) ** 2 for x in r_t1) / n
    denom = (var_t * var_t1) ** 0.5

    if denom < 1e-10:
        return

    autocorr = max(-1.0, min(1.0, cov / denom))
    fs.write_feature(
        stock_symbol, exchange, "price_mode_classifier", autocorr, as_of_date, as_of_date,
    )


# ── Beta ──────────────────────────────────────────────────────────────────────

def compute_beta_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
    market_symbol: str = "NIFTY 50",
) -> None:
    """Compute 20-day rolling beta of the stock vs the Nifty index.

    Requires NIFTY 50 daily close prices stored in the prices table with
    stock_symbol = 'NIFTY 50'.  Returns silently if Nifty data is absent or
    insufficient; the intraday index_correlation sub-signal defaults beta to 1.0.

    Worked example (5 obs):
        If stock returns are exactly 2× market returns, beta = 2.0.

    Writes:
        beta_20d — rolling 20-day beta (>1 = more volatile than market)
    """
    as_of_str = as_of_date.isoformat()
    cutoff = (as_of_date - timedelta(days=30)).isoformat()

    with _conn(db_path) as conn:
        stock_rows = conn.execute(
            """
            SELECT close FROM prices
            WHERE stock_symbol = ? AND exchange = ?
              AND trade_date > ? AND trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT 21
            """,
            (stock_symbol, exchange, cutoff, as_of_str),
        ).fetchall()

        mkt_rows = conn.execute(
            """
            SELECT close FROM prices
            WHERE stock_symbol = ? AND exchange = 'NSE'
              AND trade_date > ? AND trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT 21
            """,
            (market_symbol, cutoff, as_of_str),
        ).fetchall()

    n_obs = min(len(stock_rows), len(mkt_rows)) - 1
    if n_obs < 5:
        return

    s_cl = [float(r["close"]) for r in stock_rows[: n_obs + 1]]
    m_cl = [float(r["close"]) for r in mkt_rows[: n_obs + 1]]

    s_rets = [(s_cl[i] - s_cl[i + 1]) / s_cl[i + 1] for i in range(n_obs)]
    m_rets = [(m_cl[i] - m_cl[i + 1]) / m_cl[i + 1] for i in range(n_obs)]

    s_mean = sum(s_rets) / n_obs
    m_mean = sum(m_rets) / n_obs

    cov = sum((s_rets[i] - s_mean) * (m_rets[i] - m_mean) for i in range(n_obs)) / n_obs
    var_m = sum((r - m_mean) ** 2 for r in m_rets) / n_obs

    if var_m < 1e-10:
        return

    fs.write_feature(stock_symbol, exchange, "beta_20d", cov / var_m, as_of_date, as_of_date)


# ── Intraday: overnight news (batch-safe) ─────────────────────────────────────

def compute_overnight_news_features(
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> None:
    """Compute overnight filing sentiment for intraday pre-market context.

    Covers filings from 4:00 PM IST on (as_of_date - 1) to the current time.
    Intended to run at ~9:00 AM IST on the trading day.

    Sentiment is confidence-weighted: a filing with confidence 0.9 and label
    'positive' contributes +0.9 to the numerator.

    Writes:
        overnight_news_sentiment — confidence-weighted mean sentiment [-1, +1];
                                    positive = net bullish overnight filings.
                                    Not written if no overnight filings exist.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    prev_day = as_of_date - timedelta(days=1)
    prev_close_ist = datetime(prev_day.year, prev_day.month, prev_day.day, 16, 0, 0, tzinfo=IST)
    prev_close_utc = prev_close_ist.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT sentiment_label, sentiment_confidence
            FROM filings
            WHERE stock_symbol = ? AND exchange = ?
              AND observed_at > ? AND observed_at <= ?
            """,
            (stock_symbol, exchange, prev_close_utc, now_utc),
        ).fetchall()

    if not rows:
        return

    sentiment_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
    total_weight = 0.0
    weighted_sum = 0.0

    for row in rows:
        conf = float(row["sentiment_confidence"] or 0.5)
        score = sentiment_map.get(row["sentiment_label"] or "neutral", 0.0)
        weighted_sum += score * conf
        total_weight += conf

    if total_weight > 0:
        avg = max(-1.0, min(1.0, weighted_sum / total_weight))
        fs.write_feature(
            stock_symbol, exchange, "overnight_news_sentiment", avg, as_of_date, as_of_date,
        )
