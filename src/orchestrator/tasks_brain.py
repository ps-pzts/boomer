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


def _save_signal(db_conn: object, signal: object) -> None:
    """Insert a SignalRecord into the signals table (idempotent on signal_id)."""
    import json
    import sqlite3 as _sqlite3

    dbc: _sqlite3.Connection = db_conn  # type: ignore[assignment]
    from src.brain.models import SignalRecord

    s: SignalRecord = signal  # type: ignore[assignment]
    dbc.execute(
        """INSERT OR IGNORE INTO signals
           (signal_id, stock_symbol, exchange, track, direction, raw_score, confidence,
            regime_at_signal, contributing_signals, feature_snapshot, generated_at,
            generator_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            s.signal_id,
            s.stock_symbol,
            s.exchange,
            s.track,
            s.direction.value,
            s.raw_score,
            s.confidence,
            s.regime_at_signal,
            json.dumps([vars(cs) for cs in s.contributing_signals]),
            json.dumps(s.feature_snapshot),
            s.generated_at.isoformat(),
            s.generator_version,
        ),
    )


def _morning_batch_features(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Compute features for all NSE EQ/BE instruments as of run_date."""
    import datetime as _dt
    import sqlite3

    from src.brain.feature_store import FeatureStore
    from src.brain.features.computers import (
        compute_earnings_quality_features,
        compute_filing_sentiment_features,
        compute_price_features,
        compute_promoter_features,
        compute_smart_money_features,
    )

    as_of_date = _dt.date.fromisoformat(run_date)
    fs = FeatureStore(db_path)

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    # instruments table uses nse_symbol; series column distinguishes EQ/BE from FO/etc.
    symbols = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT nse_symbol FROM instruments"
            " WHERE series IN ('EQ','BE') AND nse_symbol IS NOT NULL ORDER BY nse_symbol"
        ).fetchall()
    ]
    conn.close()

    for sym in symbols:
        try:
            compute_price_features(db_path, fs, sym, "NSE", as_of_date)
            compute_promoter_features(db_path, fs, sym, "NSE", as_of_date)
            compute_smart_money_features(db_path, fs, sym, "NSE", as_of_date)
            compute_filing_sentiment_features(db_path, fs, sym, "NSE", as_of_date)
            compute_earnings_quality_features(db_path, fs, sym, "NSE", as_of_date)
        except Exception as exc:
            logger.warning("feature_compute_failed symbol=%s error=%s", sym, exc)

    logger.info("morning_batch_features completed symbols=%d run_date=%s", len(symbols), run_date)


def _morning_batch_signals(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Generate signals for all universe symbols and persist to signals table."""
    import datetime as _dt
    import sqlite3

    from src.brain.feature_store import FeatureStore
    from src.brain.signals.intraday import IntradaySignalGenerator
    from src.brain.signals.long_term import LongTermSignalGenerator
    from src.brain.signals.swing import SwingSignalGenerator

    as_of_date = _dt.date.fromisoformat(run_date)
    now_utc = _dt.datetime.now(_dt.UTC)
    regime = _compute_market_regime(db_path, run_date)
    fs = FeatureStore(db_path)

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    symbols = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT nse_symbol FROM instruments"
            " WHERE series IN ('EQ','BE') AND nse_symbol IS NOT NULL ORDER BY nse_symbol"
        ).fetchall()
    ]

    generators = [LongTermSignalGenerator(), SwingSignalGenerator(), IntradaySignalGenerator()]
    saved = 0
    for sym in symbols:
        try:
            features = fs.get_features_as_of(sym, "NSE", as_of_date)
            if not features:
                continue
            for gen in generators:
                signal = gen.generate(sym, "NSE", features, regime, now_utc)
                if signal is not None:
                    _save_signal(conn, signal)  # type: ignore[arg-type]
                    saved += 1
        except Exception as exc:
            logger.warning("signal_generate_failed symbol=%s error=%s", sym, exc)

    conn.commit()
    conn.close()
    logger.info(
        "morning_batch_signals completed symbols=%d signals_saved=%d run_date=%s regime=%s",
        len(symbols),
        saved,
        run_date,
        regime,
    )


def _morning_batch_recommendations(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Build trade plans from today's signals and package as recommendations."""
    import datetime as _dt
    import json
    import sqlite3
    from decimal import Decimal

    from src.brain.models import (
        ContributingSignal,
        Direction,
        SignalRecord,
    )
    from src.brain.packager import RecommendationPackager, RecommendationStore
    from src.brain.trade_decision import TradePlanGenerator
    from src.capital.risk_config import RiskConfigStore
    from src.capital.state import CapitalStateManager

    now_utc = _dt.datetime.now(_dt.UTC)

    # Load capital and risk config — both required for trade sizing.
    capital_mgr = CapitalStateManager(db_path)
    ledger = capital_mgr.latest_ledger()
    if ledger is None:
        logger.warning("morning_batch_recommendations: no capital ledger — skipping")
        return

    try:
        risk_config = RiskConfigStore(db_path).load_current()
    except Exception as exc:
        logger.warning(
            "morning_batch_recommendations: risk_config unavailable (%s) — skipping", exc
        )
        return

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    # signals table has generated_at (UTC ISO timestamp), no status column.
    today_signals = conn.execute(
        "SELECT * FROM signals WHERE generated_at LIKE ? ORDER BY confidence DESC",
        (f"{run_date}%",),
    ).fetchall()

    trade_planner = TradePlanGenerator()
    rec_store = RecommendationStore(db_path)
    packager = RecommendationPackager()

    processed = 0
    for row in today_signals:
        try:
            # Reconstruct SignalRecord from DB row.
            contributors = [
                ContributingSignal(**c) for c in json.loads(row["contributing_signals"] or "[]")
            ]
            signal = SignalRecord(
                signal_id=row["signal_id"],
                stock_symbol=row["stock_symbol"],
                exchange=row["exchange"],
                track=row["track"],
                direction=Direction(row["direction"]),
                raw_score=float(row["raw_score"]),
                confidence=float(row["confidence"]),
                regime_at_signal=row["regime_at_signal"],
                contributing_signals=contributors,
                feature_snapshot=json.loads(row["feature_snapshot"] or "{}"),
                generated_at=_dt.datetime.fromisoformat(row["generated_at"]),
                generator_version=row["generator_version"],
            )

            # Get price and ATR from feature snapshot (written by morning_batch_features).
            snapshot = signal.feature_snapshot
            current_price = Decimal(str(snapshot.get("price_close", 0)))
            atr_14d = Decimal(str(snapshot.get("atr_14d", 0)))
            if current_price <= 0 or atr_14d <= 0:
                logger.warning(
                    "recommendation_skip symbol=%s reason=missing_price_or_atr",
                    row["stock_symbol"],
                )
                continue

            from src.capital.models import Track

            track = Track(signal.track)
            bucket_capital = ledger.total_capital * ledger._allocated_pct(track)

            plan = trade_planner.generate(
                signal=signal,
                current_price=current_price,
                atr_14d=atr_14d,
                bucket_capital=bucket_capital,
                risk_config=risk_config,
                generated_at=now_utc,
            )

            if plan.decision != "proceed":
                continue

            # Compute integer position size from plan's expected risk per share.
            if plan.expected_risk_per_share > 0:
                per_trade_risk = bucket_capital * risk_config.risk_per_trade_pct(track)
                position_size = int(per_trade_risk / plan.expected_risk_per_share)
            else:
                position_size = 0

            if position_size < 1:
                continue

            # Persist trade plan before recommendation (FK constraint)
            conn.execute(
                """INSERT OR IGNORE INTO trade_plans (
                    plan_id, signal_id, stock_symbol, exchange, track, direction,
                    entry_zone_low, entry_zone_high, stop_loss_price, target_price,
                    expected_reward_per_share, expected_risk_per_share,
                    reward_to_risk, expected_value_per_share,
                    decision, skip_reason, entry_strategy_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan.plan_id,
                    plan.signal_id,
                    plan.stock_symbol,
                    plan.exchange,
                    plan.track,
                    plan.direction,
                    str(plan.entry_zone_low),
                    str(plan.entry_zone_high),
                    str(plan.stop_loss_price),
                    str(plan.target_price),
                    str(plan.expected_reward_per_share),
                    str(plan.expected_risk_per_share),
                    str(plan.reward_to_risk),
                    str(plan.expected_value_per_share),
                    plan.decision,
                    plan.skip_reason,
                    plan.entry_strategy_id.value if plan.entry_strategy_id else None,
                    now_utc.isoformat(),
                ),
            )
            conn.commit()

            rec = packager.package(
                plan=plan,
                entry_plan=None,
                signal=signal,
                position_size_shares=position_size,
            )
            # Route both swing and long-term for human approval (no auto-execution)
            from src.brain.models import RecommendationStatus

            rec.status = RecommendationStatus.AWAITING_HUMAN
            rec_store.save(rec)
            processed += 1

        except Exception as exc:
            logger.warning("recommendation_failed symbol=%s error=%s", row["stock_symbol"], exc)

    conn.commit()
    conn.close()

    logger.info(
        "morning_batch_recommendations completed signals=%d recommendations=%d run_date=%s",
        len(today_signals),
        processed,
        run_date,
    )
