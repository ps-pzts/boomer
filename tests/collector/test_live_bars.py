"""Tests for collector/live_bars.py — minute bars from ticks, parquet persistence."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from collector.live_bars import (
    LiveBarStore,
    SessionBarRecorder,
    in_session,
    read_session_bars,
)
from executor.brokers.feeds.base import Tick

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 19)


def _at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 19, h, m, s, tzinfo=IST)


def _tick(ts: datetime, ltp: float, cum: int | None = None, sym: str = "INFY", vwap=None) -> Tick:
    return Tick(
        symbol=sym,
        exchange="NSE",
        ltp=ltp,
        received_at=ts,
        volume_traded=cum,
        avg_traded_price=vwap,
    )


class TestSessionWindow:
    def test_boundaries(self):
        assert not in_session(_at(9, 14, 59))
        assert in_session(_at(9, 15, 0))
        assert in_session(_at(15, 29, 59))
        assert not in_session(_at(15, 30, 0))

    def test_pre_open_and_post_close_ticks_ignored(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 8, 0), 100.0))
        store.on_tick(_tick(_at(15, 30, 1), 100.0))
        assert store.bars("INFY") == []
        assert store.coverage_start is None

    def test_tick_from_another_day_ignored(self, tmp_path):
        store = LiveBarStore(date(2026, 11, 18), tmp_path)
        store.on_tick(_tick(_at(10, 0), 100.0))
        assert store.bars("INFY") == []


class TestBarBuilding:
    def test_ohlc_volume_and_vwap_within_one_minute(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 1), 100.0, cum=1_000, vwap=99.9))
        store.on_tick(_tick(_at(9, 15, 20), 102.0, cum=1_500, vwap=100.4))
        store.on_tick(_tick(_at(9, 15, 40), 99.0, cum=1_800))  # no vwap on this tick
        store.on_tick(_tick(_at(9, 15, 59), 101.0, cum=2_000, vwap=100.6))
        (bar,) = store.bars("INFY")
        assert bar.minute == _at(9, 15)
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 102.0, 99.0, 101.0)
        assert bar.volume == 1_000  # 2_000 - 1_000 (first bar counts from first tick)
        assert bar.cum_volume == 2_000
        assert bar.vwap == 100.6
        assert bar.ticks == 4
        assert store.coverage_start == _at(9, 15, 1)

    def test_next_bar_volume_is_delta_from_previous_bar_close(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 10), 100.0, cum=1_000))
        store.on_tick(_tick(_at(9, 15, 50), 100.5, cum=1_400))
        store.on_tick(_tick(_at(9, 16, 5), 101.0, cum=1_900))
        store.on_tick(_tick(_at(9, 16, 30), 101.2, cum=2_100))
        first, second = store.bars("INFY")
        assert first.volume == 400
        assert second.volume == 700  # 2_100 - 1_400
        assert second.open == 101.0

    def test_tick_without_volume_keeps_bar_volume(self, tmp_path):
        """Regression: a volume-less tick mid-bar must not reset volume to 0."""
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 10), 100.0, cum=1_000))
        store.on_tick(_tick(_at(9, 15, 20), 100.5, cum=1_300))
        store.on_tick(_tick(_at(9, 15, 30), 100.7, cum=None))
        (bar,) = store.bars("INFY")
        assert bar.volume == 300
        assert bar.cum_volume == 1_300
        assert bar.close == 100.7

    def test_index_ticks_have_zero_volume(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 10), 24_300.0, sym="NIFTY 50"))
        store.on_tick(_tick(_at(9, 16, 10), 24_310.0, sym="NIFTY 50"))
        assert [b.volume for b in store.bars("NIFTY 50")] == [0, 0]

    def test_gap_minutes_are_not_filled(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 10), 100.0))
        store.on_tick(_tick(_at(9, 18, 10), 101.0))
        assert [b.minute for b in store.bars("INFY")] == [_at(9, 15), _at(9, 18)]

    def test_out_of_order_tick_for_closed_bar_dropped(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 16, 10), 101.0))
        store.on_tick(_tick(_at(9, 15, 59), 90.0))
        (bar,) = store.bars("INFY")
        assert bar.low == 101.0

    def test_symbols_tracked_separately(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 10), 100.0, sym="INFY"))
        store.on_tick(_tick(_at(9, 15, 10), 50.0, sym="TCS"))
        assert store.symbols() == ["INFY", "TCS"]
        assert store.bars("TCS")[0].close == 50.0


class TestFlush:
    def test_flush_writes_only_completed_bars_and_never_twice(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 10), 100.0, cum=10))
        store.on_tick(_tick(_at(9, 16, 10), 101.0, cum=20))

        p1 = store.flush(_at(9, 16, 30))  # 09:16 bar still in progress
        assert p1 is not None and p1.name == "part-0916.parquet"
        assert [r["minute"] for r in read_session_bars(tmp_path, DAY)] == [_at(9, 15).isoformat()]

        assert store.flush(_at(9, 16, 50)) is None  # nothing new completed

        store.on_tick(_tick(_at(9, 17, 5), 102.0, cum=30))
        store.flush(_at(9, 18, 0))
        rows = read_session_bars(tmp_path, DAY)
        assert [r["minute"] for r in rows] == [
            _at(9, 15).isoformat(),
            _at(9, 16).isoformat(),
            _at(9, 17).isoformat(),
        ]
        assert rows[1]["close"] == 101.0 and rows[1]["volume"] == 10

    def test_flush_with_no_bars_writes_nothing(self, tmp_path):
        assert LiveBarStore(DAY, tmp_path).flush(_at(10, 0)) is None
        assert read_session_bars(tmp_path, DAY) == []

    def test_partition_layout(self, tmp_path):
        store = LiveBarStore(DAY, tmp_path)
        store.on_tick(_tick(_at(9, 15, 10), 100.0))
        path = store.flush(_at(15, 45))
        assert path == tmp_path / "date=2026-11-19" / "part-1545.parquet"


class TestSessionBarRecorder:
    def test_no_session_drops_ticks_and_flushes_nothing(self, tmp_path):
        rec = SessionBarRecorder(tmp_path)
        rec.on_tick(_tick(_at(9, 15, 10), 100.0))
        assert rec.store is None
        assert rec.flush(_at(10, 0)) is None

    def test_start_session_records_and_is_idempotent_for_same_day(self, tmp_path):
        rec = SessionBarRecorder(tmp_path)
        first = rec.start_session(DAY)
        rec.on_tick(_tick(_at(9, 15, 10), 100.0))
        assert rec.start_session(DAY) is first  # re-run of 08:45 task keeps today's bars
        assert len(first.bars("INFY")) == 1

    def test_new_day_rolls_store(self, tmp_path):
        """Regression guard: a process that skipped its nightly restart must not
        keep yesterday's store (every tick would be dropped on date mismatch)."""
        rec = SessionBarRecorder(tmp_path)
        rec.start_session(date(2026, 11, 18))
        rec.start_session(DAY)
        rec.on_tick(_tick(_at(9, 15, 10), 100.0))
        assert rec.store.session_date == DAY
        assert len(rec.store.bars("INFY")) == 1
