"""Scheduled task implementations: execution — broker setup, intraday, position review."""

from __future__ import annotations

import logging

from .tasks_brain import _compute_market_regime

logger = logging.getLogger(__name__)


# Feed must be streaming before the 09:15 open: the opening range is 09:15-09:29.
_FEED_MAX_TICK_AGE_SECONDS = 60


def _pre_market_executor_setup(
    run_date: str,
    run_id: int,
    db_path: str,
    brokers: list | None = None,
    feed: object | None = None,
    bar_recorder: object | None = None,
    session: object | None = None,
    **_: object,
) -> None:
    """08:45 IST: refresh broker tokens via TOTP auto-login, re-authenticate broker
    objects, start today's minute-bar session, then (re)connect the market-data
    feed with the fresh token. Creates today's Session row (`session_start`); the
    session tick moves it on once the feed is connected."""
    brokers = brokers or []
    logger.info("pre_market_executor_setup run_date=%s — refreshing broker tokens", run_date)

    try:
        from executor.auto_login import refresh_all_broker_tokens

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
                    try:
                        from alerts.alerter import get_alerter

                        get_alerter().critical(
                            f"Broker re-auth failed — {broker.broker_id}",
                            f"Token was refreshed but broker.authenticate() failed: {exc}. "
                            "Orders and market data will fail today. Check broker credentials.",
                            source_task_id="pre_market_executor_setup",
                        )
                    except Exception:
                        pass
        else:
            logger.info(
                "pre_market_executor_setup: no TOTP credentials configured — "
                "update .env with KITE_TOTP_SECRET for automated login"
            )
    except Exception as exc:
        logger.error("pre_market_executor_setup: auto_login error — %s", exc)

    try:  # #74: first run on a fresh DB: capital from the now-valid Kite token
        import datetime as _dt

        from orchestrator.bootstrap import bootstrap_capital

        bootstrap_capital(
            db_path,
            brokers,
            _dt.date.fromisoformat(run_date),
            alert=lambda t, b: _alert_critical(t, b, "pre_market_executor_setup"),
        )
    except Exception as exc:
        logger.error("pre_market_executor_setup: capital bootstrap error — %s", exc)

    if bar_recorder is not None:
        import datetime as _dt

        bar_recorder.start_session(_dt.date.fromisoformat(run_date))  # type: ignore[attr-defined]
    if feed is not None:
        _connect_market_feed(feed)
    if session is not None:
        import datetime as _dt

        session.ensure(_dt.date.fromisoformat(run_date))  # type: ignore[attr-defined]


def _minute_bar_flush(
    run_date: str, run_id: int, bar_recorder: object | None = None, now: object = None, **_: object
) -> None:
    """Every 15 min 09:00-15:45 IST: persist completed minute bars to parquet.

    Bounds what a crash can lose (design L7). The 15:45 run writes the session's
    last bars (the final bar is 15:29).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if bar_recorder is None:
        return
    now_ist = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    try:
        path = bar_recorder.flush(now_ist)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.error("minute_bar_flush failed run_date=%s error=%s", run_date, exc)
        raise  # surface to task_runner so the failure is recorded and alerted
    logger.info("minute_bar_flush run_date=%s wrote=%s", run_date, path)


def _connect_market_feed(feed: object) -> None:
    """(Re)connect the feed with whatever token is now in the environment and make
    sure the NIFTY 50 heartbeat instrument is subscribed. reauthenticate() also
    works from cold (no ticker yet), and replaces yesterday's expired session."""
    from executor.brokers.feeds.base import NIFTY_50

    try:
        feed.subscribe([NIFTY_50])  # type: ignore[attr-defined]
        feed.reauthenticate()  # type: ignore[attr-defined]
        logger.info("pre_market_executor_setup: market feed connecting")
    except Exception as exc:
        logger.error("pre_market_executor_setup: market feed connect failed — %s", exc)
        _alert_critical(
            "Market data feed failed to connect",
            f"{exc}. The live intraday pipeline cannot run without ticks.",
            "pre_market_executor_setup",
        )


def _feed_health_check(
    run_date: str, run_id: int, feed: object | None = None, now: object = None, **_: object
) -> None:
    """09:17 IST: CRITICAL alert unless the feed is connected and ticking.

    Design §3.1: never fall back silently. If this fires, the intraday pipeline
    (B7 PR 4) will see the same stale health and refuse new entries.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if feed is None:
        logger.warning("feed_health_check: no market feed configured (Kite credentials unset)")
        _alert_critical(
            "No market data feed configured",
            "KITE_API_KEY / KITE_ACCESS_TOKEN not set at startup. Intraday cannot run today.",
            "feed_health_check",
        )
        return

    now_ist = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    health = feed.health()  # type: ignore[attr-defined]
    age = health.tick_age_seconds(now_ist)
    if health.is_fresh(now_ist, _FEED_MAX_TICK_AGE_SECONDS):
        logger.info(
            "feed_health_check ok connected=%s subscribed=%d tick_age_s=%.0f",
            health.connected,
            health.subscribed,
            age,
        )
        return
    detail = (
        f"connected={health.connected} subscribed={health.subscribed} "
        f"last_tick_age_s={'never' if age is None else round(age)}"
    )
    logger.error("feed_health_check failed %s", detail)
    _alert_critical(
        "Market data feed not ticking after open",
        f"{detail}. No new intraday entries until ticks resume.",
        "feed_health_check",
    )


def _alert_critical(title: str, body: str, source_task_id: str) -> None:
    try:
        from alerts.alerter import get_alerter

        get_alerter().critical(title, body, source_task_id=source_task_id)
    except Exception as exc:
        logger.error("alert_send_failed title=%s error=%s", title, exc)


def _intraday_cycle(run_date: str, run_id: int, cycle: object = None, **_: object) -> None:
    """Every 30 min 09:00-14:30: the decision cycle (#69). It runs only while the
    session is TRADING and decides that itself (SM7), so off-hours slots are no-ops."""
    if cycle is None:
        logger.warning("intraday_cycle: no decision cycle (execution unavailable)")
        return
    result = cycle.run()  # type: ignore[attr-defined]
    logger.info(
        "intraday_cycle run_date=%s skipped=%s opened=%d rejected=%d",
        run_date,
        result.skipped,
        len(result.opened),
        len(result.rejected),
    )


def _position_review(run_date: str, run_id: int, db_path: str, **_: object) -> None:
    """Score open positions using health_score() and flag exits if thesis is broken."""
    import datetime as _dt
    import sqlite3
    from decimal import Decimal

    from brain.feature_store import FeatureStore
    from brain.models import Direction, SignalRecord
    from brain.position_review import PositionRecord, PositionReviewer

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
            current_price = Decimal(str(features.get("price_close", pos["average_entry_price"])))

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
                    pos["position_id"],
                    pos["symbol"],
                    health.total_score,
                    broken,
                    reason,
                )

        except Exception as exc:
            logger.warning(
                "position_review_failed position_id=%s error=%s", pos["position_id"], exc
            )

    conn.close()
    logger.info(
        "position_review completed open_positions=%d run_date=%s", len(open_positions), run_date
    )
