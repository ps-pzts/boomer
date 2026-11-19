"""Tests for brain/features/live.py — live features at signal time.

Includes the worked example from designs/intraday-live-pipeline.md §3.3:
prev_close 500.00, day_open 506.00 -> gap +1.20%; ORB high 509.40, low 504.20
(range 5.20), atr_14d 10.40 -> ratio 0.50 -> opening-range sub-score 0.50.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from brain.features import live
from brain.signals.intraday import IntradaySignalGenerator
from executor.brokers.feeds.base import Tick

IST = ZoneInfo("Asia/Kolkata")


def _at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 19, h, m, s, tzinfo=IST)


@dataclass
class _Bar:
    minute: datetime
    high: float
    low: float


def _tick(ltp=507.0, **kw) -> Tick:
    defaults = dict(symbol="INFY", exchange="NSE", ltp=ltp, received_at=_at(10, 0))
    defaults.update(kw)
    return Tick(**defaults)


def _orb_bars() -> list[_Bar]:
    """15 bars 09:15..09:29; extremes 509.40 (09:22) and 504.20 (09:17)."""
    bars = [_Bar(_at(9, 15) + timedelta(minutes=i), 507.0, 505.0) for i in range(15)]
    bars[7] = _Bar(_at(9, 22), 509.40, 506.0)
    bars[2] = _Bar(_at(9, 17), 506.0, 504.20)
    return bars


class TestWorkedExample:
    def test_gap(self):
        tick = _tick(day_open=506.0, prev_close=500.0)
        assert live.premarket_gap_pct(tick) == pytest.approx(1.20)

    def test_orb_ratio_and_signal_subscore(self):
        orb = live.opening_range(_orb_bars(), coverage_start=_at(9, 15, 0), now=_at(9, 30))
        assert orb == (509.40, 504.20)
        ratio = live.orb_range_vs_atr(orb, 10.40)
        assert ratio == pytest.approx(0.50)
        score = IntradaySignalGenerator()._opening_range_score({"orb_range_vs_atr": ratio})
        assert score == pytest.approx(0.50)


class TestOpeningRange:
    def test_none_before_window_closes(self):
        assert live.opening_range(_orb_bars(), _at(9, 15), now=_at(9, 29, 59)) is None

    def test_none_when_recording_started_late(self):
        """L7: after a mid-session restart the early bars are missing; don't guess."""
        assert live.opening_range(_orb_bars(), _at(9, 16, 0), now=_at(10, 0)) is None

    def test_coverage_within_first_minute_is_accepted(self):
        assert live.opening_range(_orb_bars(), _at(9, 15, 59), now=_at(10, 0)) is not None

    def test_none_without_coverage(self):
        assert live.opening_range(_orb_bars(), None, now=_at(10, 0)) is None

    def test_bars_outside_window_ignored(self):
        bars = _orb_bars() + [_Bar(_at(9, 30), 600.0, 400.0)]
        assert live.opening_range(bars, _at(9, 15), now=_at(10, 0)) == (509.40, 504.20)

    def test_none_when_symbol_never_traded_in_window(self):
        assert live.opening_range([_Bar(_at(9, 45), 1, 1)], _at(9, 15), _at(10, 0)) is None

    def test_ratio_none_without_positive_atr(self):
        assert live.orb_range_vs_atr((10.0, 9.0), 0.0) is None
        assert live.orb_range_vs_atr((10.0, 9.0), None) is None
        assert live.orb_range_vs_atr(None, 5.0) is None


class TestTickFeatures:
    def test_gap_none_before_open_print(self):
        assert live.premarket_gap_pct(_tick(prev_close=500.0)) is None
        assert live.premarket_gap_pct(None) is None

    def test_price_vs_open(self):
        assert live.price_vs_open_pct(_tick(ltp=510.0, day_open=500.0)) == pytest.approx(2.0)

    @pytest.mark.parametrize(
        ("ltp", "expected"),
        [
            (24_100.0, 1),
            (24_000.0, 0),
            (24_024.0, 0),
            (24_024.1, 1),
            (23_975.9, -1),
            (23_900.0, -1),
        ],
    )
    def test_nifty_direction_with_deadband(self, ltp, expected):
        # prev close 24_000 -> deadband 0.10% = +/- 24.0 points
        idx = _tick(symbol="NIFTY 50", ltp=ltp, prev_close=24_000.0)
        assert live.nifty_intraday_direction(idx) == expected

    def test_nifty_direction_none_without_prev_close(self):
        assert live.nifty_intraday_direction(_tick(symbol="NIFTY 50")) is None

    def test_spread(self):
        # bid 99.95, ask 100.05 -> 0.10 / 100.00 = 0.10%
        assert live.bid_ask_spread_pct(_tick(best_bid=99.95, best_ask=100.05)) == pytest.approx(
            0.10
        )

    def test_spread_none_without_depth_or_crossed(self):
        assert live.bid_ask_spread_pct(_tick(best_bid=99.0)) is None
        assert live.bid_ask_spread_pct(_tick(best_bid=101.0, best_ask=100.0)) is None

    def test_minutes_since_open_clamped_at_zero(self):
        assert live.minutes_since_market_open(_at(9, 0)) == 0.0
        assert live.minutes_since_market_open(_at(11, 0)) == pytest.approx(105.0)

    def test_minutes_since(self):
        assert live.minutes_since(_at(9, 30), _at(10, 0)) == pytest.approx(30.0)
        assert live.minutes_since(None, _at(10, 0)) is None


class TestLatestNewsAt:
    def _db(self, tmp_path, rows):
        path = tmp_path / "f.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE filings (stock_symbol TEXT, exchange TEXT, observed_at TEXT)")
        conn.executemany("INSERT INTO filings VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()
        return path

    def test_trailing_z_is_ist_not_utc(self, tmp_path):
        """L13: collector writes IST wall-clock with a misleading 'Z' suffix."""
        db = self._db(
            tmp_path,
            [
                ("INFY", "NSE", "2026-11-19T08:40:00.000000Z"),
                ("INFY", "NSE", "2026-11-18T17:00:00.000000Z"),
                ("TCS", "NSE", "2026-11-19T09:55:00.000000Z"),
            ],
        )
        got = live.latest_news_at(db, "INFY")
        assert got == _at(8, 40)
        assert live.minutes_since(got, _at(10, 0)) == pytest.approx(80.0)

    def test_none_when_no_filings(self, tmp_path):
        assert live.latest_news_at(self._db(tmp_path, []), "INFY") is None


class TestComputeLiveFeatures:
    def test_full_set_at_10am(self):
        tick = _tick(
            ltp=507.0,
            day_open=506.0,
            prev_close=500.0,
            avg_traded_price=506.5,
            best_bid=506.95,
            best_ask=507.05,
        )
        idx = _tick(symbol="NIFTY 50", ltp=24_100.0, prev_close=24_000.0)
        f = live.compute_live_features(
            tick, idx, _orb_bars(), _at(9, 15, 2), {"atr_14d": 10.40}, _at(9, 30), _at(10, 0)
        )
        assert f["premarket_gap_pct"] == pytest.approx(1.20)
        assert f["orb_high"] == 509.40 and f["orb_low"] == 504.20
        assert f["orb_range_vs_atr"] == pytest.approx(0.50)
        assert f["vwap_current"] == 506.5
        assert f["ltp"] == 507.0
        assert f["nifty_intraday_direction"] == 1.0
        assert f["minutes_since_latest_news"] == pytest.approx(30.0)
        assert f["minutes_since_market_open"] == pytest.approx(45.0)
        assert f["bid_ask_spread_pct"] == pytest.approx(0.0197, abs=1e-4)

    def test_unknown_values_omitted_not_none(self):
        """The classifier does float(features.get(k, 0.0)); an explicit None would raise."""
        f = live.compute_live_features(None, None, [], None, {}, None, _at(9, 20))
        assert f == {"minutes_since_market_open": pytest.approx(5.0)}
        assert all(v is not None for v in f.values())
