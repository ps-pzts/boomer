"""12 scheduled task definitions.

Each task function signature: fn(run_date: str, run_id: int, **kwargs) -> None
Tasks call into the appropriate subsystem modules. External I/O (brokers, HTTP)
is performed by subsystem code — tasks are thin dispatch wrappers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .models import RetryPolicy, TaskDefinition

logger = logging.getLogger(__name__)


def _prev_weekday(d: object) -> object:
    """Return the most recent weekday before d (Mon→Fri, Tue-Fri→day-1, Sat→Fri, Sun→Fri)."""
    import datetime as _dt

    day: _dt.date = d  # type: ignore[assignment]
    days_back = {0: 3, 6: 2}.get(day.weekday(), 1)  # Mon=0→3, Sun=6→2, else 1
    return day - _dt.timedelta(days=days_back)


# ─── Task implementations ──────────────────────────────────────────────────────


def _nightly_eod_collector(
    run_date: str, run_id: int, db_path: str, archive_dir: str, **_: object
) -> None:
    """Fetch EOD data from NSE/BSE: prices, filings, bulk deals, F&O OI."""
    import datetime as _dt
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path

    from src.collector.health import CollectionRunStore
    from src.collector.models import DataSource as _DataSource
    from src.collector.parser import build_fetcher_registry

    from src.collector.models import DataSource as _DataSource

    trade_date = _dt.date.fromisoformat(run_date)
    # Bulk deal files are published the next morning — always fetch the previous trading day.
    prev_trading_date = _prev_weekday(trade_date)
    _BULK_DEAL_SOURCES = {_DataSource.NSE_BULK_DEALS, _DataSource.BSE_BULK_DEALS}

    db_conn = _sqlite3.connect(db_path, timeout=10)
    store = CollectionRunStore(db_conn)
    registry = build_fetcher_registry(db=db_conn, raw_dir=_Path(archive_dir))
    for source, fetcher in registry.items():
        fetch_date = prev_trading_date if source in _BULK_DEAL_SOURCES else trade_date
        with store.run_context(source):
            fetcher.run(trade_date=fetch_date)
    db_conn.close()
    logger.info("nightly_eod_collector completed run_date=%s", run_date)


def _early_morning_data_check(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Verify yesterday's prices are present."""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT COUNT(*) as n FROM prices WHERE trade_date=?", (run_date,)
    ).fetchone()
    conn.close()
    if row["n"] == 0:
        raise RuntimeError(f"No prices for trade_date={run_date}. EOD collector may have failed.")
    logger.info("data_check passed: %d price rows for %s", row["n"], run_date)


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
    nifty_vs_200dma_pct = 1.0   # slightly above DMA — neutral
    vix_percentile = 40.0        # below median VIX — not fearful

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
        len(symbols), saved, run_date, regime,
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
    conn.close()

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
                    "recommendation_skip symbol=%s reason=missing_price_or_atr", row["stock_symbol"]
                )
                continue

            from src.capital.models import Track
            track = Track(signal.track)
            bucket_capital = ledger.total_capital * risk_config._allocated_pct(track)

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

            rec = packager.package(
                plan=plan,
                entry_plan=None,
                signal=signal,
                position_size_shares=position_size,
            )
            rec_store.save(rec)
            processed += 1

        except Exception as exc:
            logger.warning(
                "recommendation_failed symbol=%s error=%s", row["stock_symbol"], exc
            )

    logger.info(
        "morning_batch_recommendations completed signals=%d recommendations=%d run_date=%s",
        len(today_signals), processed, run_date,
    )


def _pre_market_executor_setup(
    run_date: str, run_id: int, db_path: str, brokers: list | None = None, **_: object
) -> None:
    """Refresh broker tokens via TOTP auto-login then re-authenticate broker objects."""
    brokers = brokers or []
    logger.info("pre_market_executor_setup run_date=%s — refreshing broker tokens", run_date)

    try:
        from src.executor.auto_login import refresh_all_broker_tokens

        updated = refresh_all_broker_tokens()
        if updated:
            logger.info(
                "pre_market_executor_setup: tokens refreshed brokers=%s", list(updated.keys())
            )
            # Re-authenticate live broker objects so they use the new tokens
            for broker in brokers:
                try:
                    broker.authenticate()
                    logger.info(
                        "pre_market_executor_setup: broker re-authenticated broker=%s",
                        broker.broker_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "pre_market_executor_setup: re-auth failed broker=%s error=%s",
                        broker.broker_id,
                        exc,
                    )
        else:
            logger.info(
                "pre_market_executor_setup: no TOTP credentials configured — "
                "update .env with KITE_TOTP_SECRET / FYERS_TOTP_SECRET for automated login"
            )
    except Exception as exc:
        logger.error("pre_market_executor_setup: auto_login error — %s", exc)


def _intraday_cycle(
    run_date: str, run_id: int, intraday_runner: object = None, **_: object
) -> None:
    """30-minute intraday signal→order cycle. Injected intraday_runner avoids circular imports."""
    if intraday_runner is None:
        logger.warning("intraday_cycle: no runner injected, skipping")
        return
    intraday_runner.run_cycle(run_date=run_date)  # type: ignore[attr-defined]
    logger.info("intraday_cycle completed run_date=%s", run_date)


def _position_review(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Score open positions using health_score() and flag exits if thesis is broken."""
    import datetime as _dt
    import sqlite3
    from decimal import Decimal

    from src.brain.feature_store import FeatureStore
    from src.brain.models import Direction, SignalRecord
    from src.brain.position_review import PositionRecord, PositionReviewer
    feature_store = FeatureStore(db_path)
    reviewer = PositionReviewer()

    fs = FeatureStore(db_path)
    reviewer = PositionReviewer()

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    # positions table (0004): average_entry_price, entry_at, stop_loss_price, target_price.
    # signal_id lives in trade_plans; join via trade_plan_id.
    open_positions = conn.execute(
        """SELECT p.position_id, p.symbol, p.exchange, p.track,
                  p.average_entry_price, p.entry_at,
                  p.target_price, p.stop_loss_price,
                  tp.signal_id
           FROM positions p
           LEFT JOIN trade_plans tp ON tp.plan_id = p.trade_plan_id
           WHERE p.is_open=1"""
        "SELECT position_id, symbol, track FROM positions WHERE is_open=1"
    ).fetchall()

    as_of_date = _dt.date.fromisoformat(run_date)
    regime = _compute_market_regime(db_path, run_date)

    for pos in open_positions:
        try:
            features = fs.get_features_as_of(pos["symbol"], pos["exchange"] or "NSE", as_of_date)
            current_price = Decimal(str(features.get("price_close", pos["entry_price"])))

            # Most-recent signal for this position's original signal_id.
            sig_row = conn.execute(
                "SELECT * FROM signals WHERE signal_id=?", (pos["signal_id"],)
            ).fetchone()
            current_signal: SignalRecord | None = None
            if sig_row:
                current_signal = SignalRecord(
                    signal_id=sig_row["signal_id"],
                    stock_symbol=sig_row["stock_symbol"],
                    exchange=sig_row["exchange"],
                    track=sig_row["track"],
                    direction=Direction(sig_row["direction"]),
                    raw_score=float(sig_row["raw_score"]),
                    confidence=float(sig_row["confidence"]),
                    regime_at_signal=sig_row["regime_at_signal"],
                    contributing_signals=[],
                    feature_snapshot={},
                    generated_at=_dt.datetime.fromisoformat(sig_row["generated_at"]),
                    generator_version=sig_row["generator_version"],
                )

            position = PositionRecord(
                position_id=pos["position_id"],
                stock_symbol=pos["symbol"],
                exchange=pos["exchange"] or "NSE",
                track=pos["track"],
                sector=str(features.get("sector", "unknown")),
                entry_price=Decimal(str(pos["average_entry_price"])),
                current_price=current_price,
                entry_date=_dt.datetime.fromisoformat(pos["entry_at"]),
                expected_target=Decimal(str(pos["target_price"])),
                original_stop=Decimal(str(pos["stop_loss_price"])),
                signal_id=pos["signal_id"] or "",
                original_signal_confidence=float(
                    current_signal.confidence if current_signal else 0.5
                ),
            )

            health = reviewer.health_score(position, current_signal, regime)
            broken, reason = reviewer.check_thesis_broken(position, current_signal, features)

            if health.exit_recommended or broken:
                logger.warning(
                    "position_exit_flagged position_id=%s symbol=%s score=%.1f broken=%s reason=%s",
                    pos["position_id"], pos["symbol"], health.total_score, broken, reason,
                )

        except Exception as exc:
            logger.warning(
                "position_review_failed position_id=%s error=%s", pos["position_id"], exc
            )

    conn.close()
    logger.info(
        "position_review completed open_positions=%d run_date=%s", len(open_positions), run_date
    )


def _intraday_squareoff(
    run_date: str, run_id: int, intraday_runner: object = None, **_: object
) -> None:
    """Square off intraday positions at 15:14 IST. Executor provides the method."""
    if intraday_runner is None:
        logger.warning("intraday_squareoff: no runner injected, skipping")
        return
    squared = intraday_runner.square_off_all_intraday()  # type: ignore[attr-defined]
    logger.info("intraday_squareoff completed symbols_squared_off=%s", squared)


def _eod_reconciliation(
    run_date: str,
    run_id: int,
    db_path: str,
    reconciler: object = None,
    brokers: list | None = None,
    **_: object,
) -> None:
    """Full EOD reconciliation: bot positions vs broker positions + capital sync."""
    brokers = brokers or []
    if reconciler is None and not brokers:
        logger.warning("eod_reconciliation: no reconciler or brokers configured, skipping")
        return
    if reconciler is not None:
        reconciler.run_eod(run_date=run_date)  # type: ignore[attr-defined]
    if brokers:
        from src.orchestrator.capital_sync import sync_eod_capital

        sync_eod_capital(db_path, brokers, run_date)
    logger.info("eod_reconciliation completed run_date=%s", run_date)


def _weekly_harvest_check(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Friday only: evaluate capital harvest threshold and persist if triggered."""
    import datetime as _dt

    from src.capital.harvest import SelfFundingHarvest
    from src.capital.state import CapitalStateManager

    capital_mgr = CapitalStateManager(db_path)
    ledger = capital_mgr.latest_ledger()
    if ledger is None:
        logger.warning("weekly_harvest_check: no capital ledger rows — skipping")
        return

    harvest_store = SelfFundingHarvest(db_path)
    harvest_date = _dt.date.fromisoformat(run_date)
    # Pass previous HWM (from ledger) and current total capital.
    result = harvest_store.run(
        current_total_capital=ledger.total_capital,
        previous_hwm=ledger.high_water_mark,
        harvest_date=harvest_date,
    )

    if result.fired:
        logger.info(
            "harvest_triggered amount=%.2f ops=%.2f dev=%.2f run_date=%s",
            result.harvest_amount,
            result.ops_credit,
            result.dev_credit,
            run_date,
        )
    else:
        logger.info("harvest_check: threshold not met run_date=%s", run_date)


def _nightly_backup(run_date: str, run_id: int, db_path: str, backup_dir: str, **_: object) -> None:
    """Copy SQLite DB to daily backup directory."""
    import shutil

    backup_path = Path(backup_dir) / f"{run_date}.db"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup_path)
    logger.info("nightly_backup completed backup_path=%s", backup_path)


# ─── Task registry ─────────────────────────────────────────────────────────────


def build_task_registry(
    db_path: str,
    archive_dir: str,
    backup_dir: str,
    intraday_runner: object | None = None,
    reconciler: object | None = None,
    brokers: list | None = None,
) -> dict[str, TaskDefinition]:
    """Return all 12 task definitions wired with runtime dependencies."""
    common = {"db_path": db_path, "archive_dir": archive_dir, "backup_dir": backup_dir}
    intraday_deps = {"intraday_runner": intraday_runner}
    broker_deps = {"brokers": brokers or []}
    eod_deps = {"reconciler": reconciler, "brokers": brokers or []}

    def _wrap(fn: object, extra: dict) -> object:
        import functools

        @functools.wraps(fn)  # type: ignore[arg-type]
        def wrapped(**kwargs: object) -> None:
            fn(**{**common, **extra, **kwargs})  # type: ignore[call-arg]

        return wrapped

    return {
        "nightly_eod_collector": TaskDefinition(
            task_id="nightly_eod_collector",
            fn=_wrap(_nightly_eod_collector, {}),  # type: ignore[arg-type]
            schedule="0 2 * * *",
            dependencies=[],
            timeout_seconds=1800,
            retry_policy=RetryPolicy(max_attempts=4, backoff_seconds=[300, 900, 2700]),
            run_on_holiday=True,
        ),
        "early_morning_data_check": TaskDefinition(
            task_id="early_morning_data_check",
            fn=_wrap(_early_morning_data_check, {}),  # type: ignore[arg-type]
            schedule="30 6 * * 1-5",
            dependencies=["nightly_eod_collector"],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "morning_batch_features": TaskDefinition(
            task_id="morning_batch_features",
            fn=_wrap(_morning_batch_features, {}),  # type: ignore[arg-type]
            schedule="45 6 * * 1-5",
            dependencies=["early_morning_data_check"],
            timeout_seconds=600,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "morning_batch_signals": TaskDefinition(
            task_id="morning_batch_signals",
            fn=_wrap(_morning_batch_signals, {}),  # type: ignore[arg-type]
            schedule="0 7 * * 1-5",
            dependencies=["morning_batch_features"],
            timeout_seconds=900,
            retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=[600, 600]),
        ),
        "morning_batch_recommendations": TaskDefinition(
            task_id="morning_batch_recommendations",
            fn=_wrap(_morning_batch_recommendations, {}),  # type: ignore[arg-type]
            schedule="15 7 * * 1-5",
            dependencies=["morning_batch_signals"],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "pre_market_executor_setup": TaskDefinition(
            task_id="pre_market_executor_setup",
            fn=_wrap(_pre_market_executor_setup, broker_deps),  # type: ignore[arg-type]
            schedule="0 9 * * 1-5",
            dependencies=["morning_batch_recommendations"],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "intraday_cycle": TaskDefinition(
            task_id="intraday_cycle",
            fn=_wrap(_intraday_cycle, intraday_deps),  # type: ignore[arg-type]
            schedule="*/30 9-14 * * 1-5",
            dependencies=[],
            timeout_seconds=180,
            retry_policy=RetryPolicy(max_attempts=1),  # no retry — next cycle in 30 min
        ),
        "position_review": TaskDefinition(
            task_id="position_review",
            fn=_wrap(_position_review, {}),  # type: ignore[arg-type]
            schedule="0 9-15 * * 1-5",
            dependencies=[],
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=1),
            trailing_stop_task=True,  # runs even in paused mode
        ),
        "intraday_squareoff": TaskDefinition(
            task_id="intraday_squareoff",
            fn=_wrap(_intraday_squareoff, intraday_deps),  # type: ignore[arg-type]
            schedule="44 9 * * 1-5",  # 15:14 IST = 09:44 UTC
            dependencies=[],
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "eod_reconciliation": TaskDefinition(
            task_id="eod_reconciliation",
            fn=_wrap(_eod_reconciliation, eod_deps),  # type: ignore[arg-type]
            schedule="30 10 * * 1-5",  # 16:00 IST = 10:30 UTC
            dependencies=[],
            timeout_seconds=600,
            retry_policy=RetryPolicy(max_attempts=6, backoff_seconds=[600, 600, 600, 600, 600]),
        ),
        "weekly_harvest_check": TaskDefinition(
            task_id="weekly_harvest_check",
            fn=_wrap(_weekly_harvest_check, {}),  # type: ignore[arg-type]
            schedule="0 11 * * 5",  # 16:30 IST Friday = 11:00 UTC
            dependencies=["eod_reconciliation"],
            timeout_seconds=120,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        "nightly_backup": TaskDefinition(
            task_id="nightly_backup",
            fn=_wrap(_nightly_backup, {}),  # type: ignore[arg-type]
            schedule="30 17 * * *",  # 23:00 IST = 17:30 UTC
            dependencies=[],
            timeout_seconds=900,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=[300]),
            run_on_holiday=True,
        ),
    }
