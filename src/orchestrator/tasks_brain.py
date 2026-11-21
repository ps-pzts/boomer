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

    from brain.regime import RegimeDetector

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

    from brain.feature_store import FeatureStore
    from brain.features.runner import run_track_computers
    from brain.features.track_intraday import INTRADAY_COMPUTERS
    from brain.universe import intraday_universe

    as_of_date = _dt.date.fromisoformat(run_date)
    fs = FeatureStore(db_path)
    # Only the F&O universe can ever be traded (#50): no point computing the rest.
    symbols = intraday_universe(db_path, as_of_date).symbols

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

    from brain.feature_store import FeatureStore
    from brain.universe import intraday_universe
    from brain.watchlist import WatchlistStore, select_watchlist
    from capital.risk_config import RiskConfigStore

    session_date = _dt.date.fromisoformat(run_date)
    now_ist = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))
    risk_config = RiskConfigStore(db_path).load_current()
    regime = _compute_market_regime(db_path, run_date)
    fs = FeatureStore(db_path)

    universe = intraday_universe(db_path, session_date)
    if universe.unavailable_reason:
        # Fail closed: never fall back to the unfiltered equity universe (#50).
        logger.error(
            "intraday_watchlist universe_unavailable run_date=%s reason=%s",
            run_date,
            universe.unavailable_reason,
        )
        WatchlistStore(db_path).save(session_date, [])
        _alert_critical_brain(
            "No F&O universe: not trading today",
            f"{universe.unavailable_reason} (F&O data as of {universe.fno_as_of}). "
            "Check the nightly F&O bhavcopy collection.",
        )
        return
    _alert_universe_notices(universe)
    symbols = universe.symbols

    def _features():
        for sym in symbols:
            features = fs.get_features_as_of(sym, "NSE", session_date)
            if features:
                yield sym, "NSE", features

    entries = select_watchlist(
        _features(),
        risk_config,
        regime,
        now_ist,
        universe_rejections={
            "not_fno": universe.not_fno_count,
            "under_surveillance": len(universe.under_surveillance),
        },
    )
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


def _alert_universe_notices(universe: object) -> None:
    """Surface a degraded surveillance filter or symbol mismatches (#50). The filter is
    never silently dropped: stale -> WARN, unavailable -> CRITICAL (trading F&O-only)."""
    from alerts.alerter import get_alerter

    status = universe.surveillance_status  # type: ignore[attr-defined]
    asof, age = universe.surveillance_as_of, universe.surveillance_age  # type: ignore[attr-defined]
    try:
        if status == "stale":
            get_alerter().warn(
                "ASM/GSM list is stale",
                f"Last surveillance list is from {asof} ({age} trading days old); still "
                "applied. Check the nightly surveillance fetch.",
                source_task_id="intraday_watchlist",
            )
        elif status == "unavailable":
            _alert_critical_brain(
                "ASM/GSM list unavailable: trading F&O-only",
                f"No usable surveillance list (last: {asof}, age: {age} trading days; "
                "usable only up to 3). Stocks under ASM/GSM are NOT excluded today. "
                "Fix the NSE surveillance fetch; it is reconciled as soon as a list arrives.",
            )
        unmatched = universe.fno_unmatched  # type: ignore[attr-defined]
        if unmatched:
            get_alerter().warn(
                "F&O underlyings with no equity match",
                f"{len(unmatched)} F&O stock(s) match no EQ instrument (renamed or series "
                f"change?): {', '.join(unmatched[:15])}. They are not tradable today.",
                source_task_id="intraday_watchlist",
            )
    except Exception as exc:
        logger.error("alert_send_failed title=universe_notice error=%s", exc)


def _alert_critical_brain(title: str, body: str) -> None:
    try:
        from alerts.alerter import get_alerter

        get_alerter().critical(title, body, source_task_id="intraday_watchlist")
    except Exception as exc:
        logger.error("alert_send_failed title=%s error=%s", title, exc)
