"""Scheduled task implementations: execution — broker setup, intraday, position review."""

from __future__ import annotations

import logging

from .tasks_brain import _compute_market_regime

logger = logging.getLogger(__name__)


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
    ).fetchall()

    as_of_date = _dt.date.fromisoformat(run_date)
    regime = _compute_market_regime(db_path, run_date)

    for pos in open_positions:
        try:
            features = fs.get_features_as_of(pos["symbol"], pos["exchange"] or "NSE", as_of_date)
            current_price = Decimal(
                str(features.get("price_close", pos["average_entry_price"]))
            )

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
                    "position_exit_flagged position_id=%s symbol=%s score=%.1f "
                    "broken=%s reason=%s",
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
