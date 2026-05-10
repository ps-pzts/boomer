from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from executor.intraday import IntradayPipeline


class TestIntradaySignalValidity:
    def test_signal_within_30_min_is_valid(self):
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=MagicMock())
        generated = datetime.now(UTC)
        assert pipeline.is_signal_still_valid("RELIANCE", generated) is True

    def test_signal_older_than_30_min_is_invalid(self):
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=MagicMock())
        from datetime import timedelta

        generated = datetime.now(UTC) - timedelta(minutes=31)
        assert pipeline.is_signal_still_valid("RELIANCE", generated) is False


class TestIntradayCooldown:
    def test_no_cooldown_initially(self):
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=MagicMock())
        assert pipeline.is_in_cooldown("TCS") is False

    def test_in_cooldown_after_signal_acted(self):
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=MagicMock())
        pipeline.record_signal_acted("INFY")
        assert pipeline.is_in_cooldown("INFY") is True

    def test_different_stocks_have_independent_cooldowns(self):
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=MagicMock())
        pipeline.record_signal_acted("HDFC")
        assert pipeline.is_in_cooldown("HDFC") is True
        assert pipeline.is_in_cooldown("ICICI") is False


class TestIntradayCycleLock:
    def test_second_cycle_skipped_when_first_running(self):
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=MagicMock())
        # Acquire lock manually to simulate running cycle
        pipeline._cycle_lock.acquire()
        try:
            result = pipeline.run_cycle()
            assert result is False  # skipped
        finally:
            pipeline._cycle_lock.release()

    def test_disabled_pipeline_returns_false(self):
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=MagicMock())
        pipeline._disabled_for_day = True
        assert pipeline.run_cycle() is False


class TestSquareOffWindow:
    def test_squareoff_outside_window_returns_empty(self):
        pm = MagicMock()
        pm.load_open.return_value = []
        pipeline = IntradayPipeline(order_manager=MagicMock(), position_manager=pm)
        # At midnight UTC (not in squareoff window 09:30-09:50 UTC = 15:00-15:20 IST)
        result = pipeline.square_off_all_intraday()
        assert result == []
