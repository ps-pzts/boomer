"""Tests for the market-feed parts of orchestrator/tasks_executor.py and their wiring."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.executor.brokers.feeds.base import NIFTY_50, Tick
from src.executor.brokers.feeds.manual_feed import ManualFeed
from src.orchestrator.tasks import build_task_registry
from src.orchestrator.tasks_executor import _feed_health_check, _pre_market_executor_setup

IST = ZoneInfo("Asia/Kolkata")
OPEN_PLUS_2 = datetime(2026, 11, 19, 9, 17, 0, tzinfo=IST)


@pytest.fixture()
def alerter():
    fake = MagicMock()
    with patch("src.alerts.alerter.get_alerter", return_value=fake):
        yield fake


def _feed_with_tick(age_seconds: float) -> ManualFeed:
    feed = ManualFeed()
    feed.connect()
    feed.push(
        Tick(
            symbol=NIFTY_50,
            exchange="NSE",
            ltp=24_300.0,
            received_at=OPEN_PLUS_2 - timedelta(seconds=age_seconds),
        )
    )
    return feed


class TestFeedHealthCheck:
    def test_fresh_feed_no_alert(self, alerter):
        _feed_health_check("2026-11-19", 1, feed=_feed_with_tick(5), now=OPEN_PLUS_2)
        alerter.critical.assert_not_called()

    def test_stale_feed_alerts_critical(self, alerter):
        _feed_health_check("2026-11-19", 1, feed=_feed_with_tick(120), now=OPEN_PLUS_2)
        alerter.critical.assert_called_once()
        assert "not ticking" in alerter.critical.call_args.args[0]
        assert "last_tick_age_s=120" in alerter.critical.call_args.args[1]

    def test_never_ticked_alerts_critical(self, alerter):
        feed = ManualFeed()
        feed.connect()
        _feed_health_check("2026-11-19", 1, feed=feed, now=OPEN_PLUS_2)
        assert "last_tick_age_s=never" in alerter.critical.call_args.args[1]

    def test_disconnected_feed_alerts_even_with_recent_tick(self, alerter):
        feed = _feed_with_tick(5)
        feed.close()
        _feed_health_check("2026-11-19", 1, feed=feed, now=OPEN_PLUS_2)
        alerter.critical.assert_called_once()

    def test_no_feed_configured_alerts_critical(self, alerter):
        _feed_health_check("2026-11-19", 1, feed=None, now=OPEN_PLUS_2)
        assert "No market data feed" in alerter.critical.call_args.args[0]


class TestPreMarketSetupConnectsFeed:
    def test_subscribes_nifty_and_reauthenticates_after_token_refresh(self, alerter):
        feed = MagicMock()
        order: list[str] = []
        with patch(
            "src.executor.auto_login.refresh_all_broker_tokens",
            side_effect=lambda: order.append("refresh") or {"kite": "t"},
        ):
            feed.reauthenticate.side_effect = lambda: order.append("feed")
            _pre_market_executor_setup("2026-11-19", 1, db_path=":memory:", brokers=[], feed=feed)
        feed.subscribe.assert_called_once_with([NIFTY_50])
        assert order == ["refresh", "feed"]  # feed must use the fresh token
        alerter.critical.assert_not_called()

    def test_feed_connect_failure_alerts_critical(self, alerter):
        feed = MagicMock()
        feed.reauthenticate.side_effect = RuntimeError("ws refused")
        with patch("src.executor.auto_login.refresh_all_broker_tokens", return_value={}):
            _pre_market_executor_setup("2026-11-19", 1, db_path=":memory:", brokers=[], feed=feed)
        assert "failed to connect" in alerter.critical.call_args.args[0]

    def test_no_feed_is_a_no_op(self, alerter):
        with patch("src.executor.auto_login.refresh_all_broker_tokens", return_value={}):
            _pre_market_executor_setup("2026-11-19", 1, db_path=":memory:", brokers=[], feed=None)
        alerter.critical.assert_not_called()


class TestSchedule:
    def _tasks(self, feed=None):
        return build_task_registry(":memory:", "/tmp/a", "/tmp/b", feed=feed)

    def test_setup_runs_before_open_with_no_dependencies(self):
        td = self._tasks()["pre_market_executor_setup"]
        assert td.schedule == "45 8 * * 1-5"
        assert td.dependencies == []

    def test_feed_health_check_after_open_and_after_setup(self):
        td = self._tasks()["feed_health_check"]
        assert td.schedule == "17 9 * * 1-5"
        assert td.dependencies == ["pre_market_executor_setup"]

    def test_feed_injected_into_health_check(self, alerter):
        feed = _feed_with_tick(5)
        self._tasks(feed=feed)["feed_health_check"].fn(run_date="2026-11-19", run_id=1)
        # The injected feed was used: a fresh feed at real "now" may be stale, but a
        # missing feed would have produced the "No market data feed" alert instead.
        for call in alerter.critical.call_args_list:
            assert "No market data feed" not in call.args[0]
