"""Scheduled task implementations: brain — features, signals, recommendations."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _compute_market_regime(db_path: str, run_date: str) -> str:
    """Estimate market regime from prices table.

    Computes breadth (% of NSE stocks above 50 DMA) from rolling prices.
    VIX percentile and Nifty vs 200 DMA are not yet in the DB — defaults used.
    Returns the regime value string (e.g. 'bull_calm').
    """
    import sqlite3

    from src.brain.regime import RegimeDetector

    conn = sqlite3.connect(db_path, timeout=5)
    try:
        # Breadth: % of symbols whose latest close > 50-day SMA
        rows = conn.execute(
            """
            SELECT stock_symbol,
                   AVG(close) AS sma50,
                   (SELECT close FROM prices p2
                    WHERE p2.stock_symbol = p1.stock_symbol
                      AND p2.exchange = p1.exchange
                    ORDER BY trade_date DESC LIMIT 1) AS latest_close
            FROM prices p1
            WHERE exchange = 'NSE'
              AND trade_date <= ?
            GROUP BY stock_symbol, exchange
            HAVING COUNT(*) >= 10
            """,
            (run_date,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        logger.warning("regime_compute: no price rows — defaulting to SIDEWAYS")
        return "sideways"

    above = sum(1 for r in rows if r[2] is not None and r[2] > r[1])
    breadth_pct = (above / len(rows)) * 100.0

    # VIX and Nifty vs 200DMA require data sources not yet collected; use neutral defaults.
    nifty_vs_200dma_pct = 1.0  # slightly above DMA — neutral
    vix_percentile = 40.0  # below median VIX — not fearful

    detector = RegimeDetector()
    regime = detector.detect(
        nifty_vs_200dma_pct=nifty_vs_200dma_pct,
        vix_percentile=vix_percentile,
        breadth_pct=breadth_pct,
        recent_regimes=[],  # no stickiness on first run
    )
    logger.info(
        "regime_compute breadth_pct=%.1f regime=%s run_date=%s", breadth_pct, regime, run_date
    )
    return regime.value


def _morning_batch_features(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Compute batch features for all NSE EQ/BE instruments as of run_date.

    Runs the intraday track's own registry (price, F&O OI, beta, overnight
    news) — the only track this system trades. Live-only features (premarket
    gap, ORB, index direction, spread, news recency) are injected separately
    at signal time, not here.
    """
    import datetime as _dt
    import sqlite3

    from src.brain.feature_store import FeatureStore
    from src.brain.features.runner import run_track_computers
    from src.brain.features.track_intraday import INTRADAY_COMPUTERS

    as_of_date = _dt.date.fromisoformat(run_date)
    fs = FeatureStore(db_path)

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    # instruments table uses nse_symbol; series column distinguishes EQ/BE from FO/etc.
    symbols = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT nse_symbol FROM instruments"
            " WHERE series = 'EQ' AND nse_symbol IS NOT NULL ORDER BY nse_symbol"
        ).fetchall()
    ]
    conn.close()

    for sym in symbols:
        try:
            run_track_computers(INTRADAY_COMPUTERS, db_path, fs, sym, "NSE", as_of_date)
        except Exception as exc:
            logger.warning("feature_compute_failed symbol=%s error=%s", sym, exc)

    logger.info("morning_batch_features completed symbols=%d run_date=%s", len(symbols), run_date)


def _intraday_watchlist(
    run_date: str, run_id: int, db_path: str, feed: object | None = None, **_: object
) -> None:
    """09:10 IST: pick today's watchlist (design §3.6), persist it, and subscribe it on
    the market feed so bars and live features exist from the 09:15 open.

    Replaces morning_batch_signals / morning_batch_recommendations (#37): their
    recommendations scored stale data only and nothing has executed them since #26.
    """
    import datetime as _dt
    import sqlite3

    from src.brain.feature_store import FeatureStore
    from src.brain.watchlist import WatchlistStore, select_watchlist
    from src.capital.risk_config import RiskConfigStore

    session_date = _dt.date.fromisoformat(run_date)
    now_ist = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))
    risk_config = RiskConfigStore(db_path).load_current()
    regime = _compute_market_regime(db_path, run_date)
    fs = FeatureStore(db_path)

    conn = sqlite3.connect(db_path, timeout=5)
    symbols = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT nse_symbol FROM instruments"
            " WHERE series = 'EQ' AND nse_symbol IS NOT NULL ORDER BY nse_symbol"
        ).fetchall()
    ]
    conn.close()

    def _features():
        for sym in symbols:
            features = fs.get_features_as_of(sym, "NSE", session_date)
            if features:
                yield sym, "NSE", features

    entries = select_watchlist(_features(), risk_config, regime, now_ist)
    WatchlistStore(db_path).save(session_date, entries)
    if not entries:
        logger.error("intraday_watchlist empty run_date=%s universe=%d", run_date, len(symbols))
        _alert_critical_brain(
            "Intraday watchlist is empty",
            f"No eligible stock out of {len(symbols)} for {run_date}. Check the 09:05 features.",
        )
        return
    if feed is not None:
        feed.subscribe([e.stock_symbol for e in entries])  # type: ignore[attr-defined]
    logger.info(
        "intraday_watchlist completed run_date=%s size=%d subscribed=%s",
        run_date,
        len(entries),
        feed is not None,
    )


def _alert_critical_brain(title: str, body: str) -> None:
    try:
        from src.alerts.alerter import get_alerter

        get_alerter().critical(title, body, source_task_id="intraday_watchlist")
    except Exception as exc:
        logger.error("alert_send_failed title=%s error=%s", title, exc)
