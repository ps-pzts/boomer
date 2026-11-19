"""Tests for feeds/base.py (FeedHealth) and feeds/manual_feed.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from executor.brokers.feeds.base import FeedHealth, Tick
from executor.brokers.feeds.manual_feed import ManualFeed

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 19, 10, 0, 0, tzinfo=IST)


class TestFeedHealth:
    def test_fresh_when_connected_and_recent(self) -> None:
        h = FeedHealth(connected=True, last_tick_at=NOW - timedelta(seconds=30), subscribed=5)
        assert h.tick_age_seconds(NOW) == 30
        assert h.is_fresh(NOW, max_age_seconds=60)

    def test_boundary_age_equal_to_max_is_fresh(self) -> None:
        h = FeedHealth(connected=True, last_tick_at=NOW - timedelta(seconds=60), subscribed=1)
        assert h.is_fresh(NOW, max_age_seconds=60)

    def test_stale_tick_not_fresh(self) -> None:
        h = FeedHealth(connected=True, last_tick_at=NOW - timedelta(seconds=61), subscribed=1)
        assert not h.is_fresh(NOW, max_age_seconds=60)

    def test_never_ticked_not_fresh(self) -> None:
        h = FeedHealth(connected=True, last_tick_at=None, subscribed=1)
        assert h.tick_age_seconds(NOW) is None
        assert not h.is_fresh(NOW, max_age_seconds=60)

    def test_disconnected_not_fresh_even_with_recent_tick(self) -> None:
        h = FeedHealth(connected=False, last_tick_at=NOW, subscribed=1)
        assert not h.is_fresh(NOW, max_age_seconds=60)


class TestManualFeed:
    def test_push_updates_latest_health_and_callbacks(self) -> None:
        feed = ManualFeed()
        feed.connect()
        seen: list[Tick] = []
        feed.on_tick(seen.append)
        tick = Tick(symbol="INFY", exchange="NSE", ltp=1500.0, received_at=NOW)
        feed.push(tick)
        assert seen == [tick]
        assert feed.latest("INFY") == tick
        assert feed.health().last_tick_at == NOW
        assert feed.health().connected is True

    def test_subscribe_tracks_symbols(self) -> None:
        feed = ManualFeed()
        feed.subscribe(["A", "B"])
        feed.subscribe(["B", "C"])
        assert feed.subscribed == {"A", "B", "C"}
        assert feed.health().subscribed == 3

    def test_capabilities_reflect_depth_flag(self) -> None:
        assert ManualFeed(has_depth=False).capabilities().has_depth is False
