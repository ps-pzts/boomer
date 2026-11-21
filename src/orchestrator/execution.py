"""Execution wiring for the trading day (#63, designs/intraday-live-pipeline.md §5).

Builds, once per process: the execution broker the trading mode chose (paper
fills on the live feed, or Kite), OrderManager + PositionManager +
TradeSupervisor on ONE SQLite connection, and the SessionController that drives
them. Tasks run on different threads, so the connection is opened with
`check_same_thread=False` and every use goes through `SessionController.lock`.

Fails closed: no feed, no capital ledger, or a broker that can't be built →
no supervisor. The session still runs (and abandons or stays out of TRADING
with `execution_unavailable`), so the day is visible in observability.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

logger = logging.getLogger(__name__)


class FeedLtp:
    """The `ltp_cache` OrderManager reads for its price-band check, backed by the feed."""

    def __init__(self, feed: object) -> None:
        self._feed = feed

    def get(self, symbol: str, default: float | None = None) -> float | None:
        tick = self._feed.latest(symbol)  # type: ignore[attr-defined]
        return tick.ltp if tick is not None else default


def build_session(
    db_path: str,
    mode: str | None,
    feed: object | None,
    brokers: list,
    alerter: object | None = None,
) -> object:
    """Return the SessionController for this process (with or without execution).
    mode None (production): resolved per session day and locked in its row (#76)."""
    from capital.risk_config import RiskConfigStore
    from capital.state import CapitalStateManager
    from executor.session_controller import SessionController
    from orchestrator.models import BotModeStore

    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    capital = CapitalStateManager(db_path)

    def execution(day_mode: str) -> tuple | None:  # retried each tick (#74), per day (#76)
        built = _build_execution(conn, db_path, day_mode, feed, brokers, capital, alerter)
        return built if built[0] is not None else None

    def watchlist_size(day: date) -> int:
        from brain.watchlist import WatchlistStore

        try:
            return len(WatchlistStore(db_path).load(day))
        except Exception as exc:  # table missing / locked: no watchlist = no trading
            logger.warning("session_watchlist_unavailable error=%s", exc)
            return 0

    def mode_for(day: date) -> str:  # resolved when the day's session starts (#76)
        import os

        from executor.trading_mode import ENV_VAR, session_mode

        try:
            return session_mode(db_path, day, os.environ.get(ENV_VAR))
        except Exception as exc:  # fail closed
            logger.error("mode_resolution_failed error=%s (paper)", exc)
            return "paper"

    return SessionController(
        conn,
        mode=mode_for if mode is None else mode,
        feed=feed,
        supervisor=None,
        broker=None,
        bot_mode=BotModeStore(db_path).current_mode,
        watchlist_size=watchlist_size,
        ledger=capital.latest_ledger,
        risk_config=RiskConfigStore(db_path).load_current,
        alerter=alerter,
        execution_factory=execution,
    )


def _build_execution(
    conn: sqlite3.Connection,
    db_path: str,
    mode: str,
    feed: object | None,
    brokers: list,
    capital: object,
    alerter: object | None,
) -> tuple[object | None, object | None, object | None]:
    from executor.brokers.paper_book import IST, PaperBook
    from executor.models import BrokerName
    from executor.order_manager import OrderManager
    from executor.position_manager import PositionManager
    from executor.trade_lifecycle import TradeSupervisor
    from executor.trading_mode import TradingMode, build_execution_broker

    if feed is None:
        logger.warning("execution_unavailable reason=no_market_feed")
        return None, None, None
    try:
        ledger = capital.latest_ledger()  # type: ignore[attr-defined]
        if ledger is None:
            raise RuntimeError("no capital ledger: initialise capital first")
        live = next((b for b in brokers if b.broker_id == BrokerName.KITE), None)
        broker = build_execution_broker(
            TradingMode(mode),
            feed,
            live,
            paper_initial_cash=float(ledger.total_capital),
            paper_book=PaperBook(db_path, datetime.now(IST).date()),  # #64: restart-safe
        )
    except Exception as exc:
        logger.warning("execution_unavailable reason=%s (retried next tick)", exc)
        return None, None, None
    ltp = FeedLtp(feed)
    om = OrderManager(conn, {BrokerName.KITE: broker}, ltp_cache=ltp, alerter=alerter)
    pm = PositionManager(conn, gtt_manager=None, order_manager=om)  # type: ignore[arg-type]
    logger.info("execution_ready mode=%s broker=%s", mode, broker.broker_id)
    return TradeSupervisor(conn, om, pm, alerter, price=ltp.get), broker, om


def build_decision_cycle(
    db_path: str, session: object | None, feed: object | None, bar_recorder: object | None
) -> object | None:
    """The 30-minute decision cycle (#69). None without a session or a feed. It uses
    the session's execution when it runs, so it waits for a lazily built one (#74)."""
    if session is None or feed is None:
        logger.error("decision_cycle_unavailable (no session / feed)")
        return None
    from brain.feature_store import FeatureStore
    from brain.features.live import latest_news_at
    from brain.surveillance import blocked_symbols
    from brain.watchlist import WatchlistStore
    from capital.risk_config import RiskConfigStore
    from capital.state import CapitalStateManager
    from executor.decision_cycle import DecisionCycle
    from orchestrator.tasks_brain import _compute_market_regime

    fs = FeatureStore(db_path)
    regimes: dict[date, str] = {}

    def regime(day: date) -> str:  # prices don't change intraday: once per day
        if day not in regimes:
            regimes[day] = _compute_market_regime(db_path, day.isoformat())
        return regimes[day]

    return DecisionCycle(
        db_path=db_path,
        session=session,
        feed=feed,
        bar_store=lambda: getattr(bar_recorder, "store", None),
        batch_features=fs.get_features_as_of,
        latest_news=lambda sym: latest_news_at(db_path, sym),
        regime=regime,
        risk_config=RiskConfigStore(db_path).load_current,
        ledger=CapitalStateManager(db_path).latest_ledger,
        watchlist=WatchlistStore(db_path).load,
        surveillance_blocked=lambda day: blocked_symbols(db_path, day),
    )
