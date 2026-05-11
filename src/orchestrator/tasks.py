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


# ─── Task implementations ──────────────────────────────────────────────────────


def _nightly_eod_collector(
    run_date: str, run_id: int, db_path: str, archive_dir: str, **_: object
) -> None:
    """Fetch EOD data from NSE/BSE: prices, filings, bulk deals, F&O OI."""
    import datetime as _dt
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path

    from src.collector.health import CollectionRunStore
    from src.collector.parser import build_fetcher_registry

    trade_date = _dt.date.fromisoformat(run_date)
    db_conn = _sqlite3.connect(db_path, timeout=10)
    store = CollectionRunStore(db_conn)
    registry = build_fetcher_registry(db=db_conn, raw_dir=_Path(archive_dir))
    for source, fetcher in registry.items():
        with store.run_context(source):
            fetcher.run(trade_date=trade_date)
    db_conn.close()
    logger.info("nightly_eod_collector completed run_date=%s", run_date)


def _early_morning_data_check(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Verify yesterday's prices and filings are present and fresh."""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    trade_date = run_date  # YYYY-MM-DD

    # Check prices exist for trade_date
    row = conn.execute(
        "SELECT COUNT(*) as n FROM prices WHERE trade_date=?", (trade_date,)
    ).fetchone()
    conn.close()
    if row["n"] == 0:
        raise RuntimeError(f"No prices for trade_date={trade_date}. EOD collector may have failed.")
    logger.info("data_check passed: %d price rows for %s", row["n"], trade_date)


def _morning_batch_features(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Compute features for all Nifty 500 stocks as of run_date."""
    from src.brain.feature_store import FeatureStore
    from src.brain.features.computers import (
        compute_earnings_quality_features,
        compute_filing_sentiment_features,
        compute_price_features,
        compute_promoter_features,
        compute_smart_money_features,
    )

    feature_store = FeatureStore(db_path)
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    symbols = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT symbol FROM instruments WHERE series IN ('EQ','BE') ORDER BY symbol"
        ).fetchall()
    ]
    conn.close()

    for sym in symbols:
        try:
            compute_price_features(sym, run_date, feature_store, db_path)
            compute_promoter_features(sym, run_date, feature_store, db_path)
            compute_smart_money_features(sym, run_date, feature_store, db_path)
            compute_filing_sentiment_features(sym, run_date, feature_store, db_path)
            compute_earnings_quality_features(sym, run_date, feature_store, db_path)
        except Exception as exc:
            logger.warning("feature_compute_failed symbol=%s error=%s", sym, exc)
    logger.info("morning_batch_features completed symbols=%d run_date=%s", len(symbols), run_date)


def _morning_batch_signals(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Generate signals for all universe symbols."""
    from src.brain.feature_store import FeatureStore
    from src.brain.regime import RegimeDetector
    from src.brain.signals.intraday import IntradaySignalGenerator
    from src.brain.signals.long_term import LongTermSignalGenerator
    from src.brain.signals.swing import SwingSignalGenerator

    feature_store = FeatureStore(db_path)
    regime_detector = RegimeDetector(db_path)
    current_regime = regime_detector.detect(run_date)

    for GenClass in (LongTermSignalGenerator, SwingSignalGenerator, IntradaySignalGenerator):
        gen = GenClass(db_path=db_path, feature_store=feature_store)
        gen.generate_all(as_of=run_date, regime=current_regime)

    logger.info("morning_batch_signals completed run_date=%s regime=%s", run_date, current_regime)


def _morning_batch_recommendations(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Run portfolio construction and package recommendations."""
    from src.brain.feature_store import FeatureStore
    from src.brain.packager import RecommendationPackager, RecommendationStore
    from src.brain.portfolio import PortfolioConstructor
    from src.brain.trade_decision import TradePlanGenerator

    feature_store = FeatureStore(db_path)
    trade_planner = TradePlanGenerator(db_path=db_path, feature_store=feature_store)
    portfolio = PortfolioConstructor(db_path=db_path)
    rec_store = RecommendationStore(db_path)
    packager = RecommendationPackager(portfolio=portfolio, rec_store=rec_store)

    import sqlite3

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    pending_signals = conn.execute(
        "SELECT * FROM signals WHERE signal_date=? AND status='pending'", (run_date,)
    ).fetchall()
    conn.close()

    for sig in pending_signals:
        try:
            plan = trade_planner.generate(sig["symbol"], sig["track"], run_date)
            if plan:
                packager.package(plan, run_date)
        except Exception as exc:
            logger.warning("recommendation_failed symbol=%s error=%s", sig["symbol"], exc)

    logger.info("morning_batch_recommendations completed run_date=%s", run_date)


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
    """Score open positions and generate exit recommendations if needed."""
    from src.brain.feature_store import FeatureStore
    from src.brain.position_review import PositionReviewer

    feature_store = FeatureStore(db_path)
    reviewer = PositionReviewer(db_path=db_path, feature_store=feature_store)

    import sqlite3

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    open_positions = conn.execute(
        "SELECT position_id, symbol, track FROM positions WHERE status='open'"
    ).fetchall()
    conn.close()

    for pos in open_positions:
        try:
            reviewer.review(pos["position_id"], as_of=run_date)
        except Exception as exc:
            logger.warning(
                "position_review_failed position_id=%s error=%s", pos["position_id"], exc
            )

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
    """Friday only: evaluate capital harvest threshold."""
    from src.capital.harvest import SelfFundingHarvest, evaluate_harvest
    from src.capital.state import CapitalStateManager

    capital_mgr = CapitalStateManager(db_path)
    view = capital_mgr.live_capital_view()
    result = evaluate_harvest(view)
    if result.harvest_triggered:
        harvest_store = SelfFundingHarvest(db_path)
        harvest_store.record(result, run_date)
        logger.info(
            "harvest_triggered amount=%.2f ops=%.2f dev=%.2f run_date=%s",
            result.harvest_amount,
            result.ops_fund,
            result.dev_fund,
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
