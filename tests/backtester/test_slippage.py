from __future__ import annotations

import pytest

from backtester.slippage import SlippageModel
from executor.models import OrderSide, OrderType


class TestSlippageMarketOrder:
    """
    Worked example from Phase 4 design doc:
    Market buy at ₹500 on liquid stock:
        base_slippage = 0.05% = ₹0.25
        liquidity_adj = 1.0 (small quantity)
        volatility_adj = 1.0 (ATR = 2%)
        fill_price = ₹500.25
        slippage_bps = 5.0
    """

    def test_market_buy_adds_slippage(self):
        sm = SlippageModel()
        result = sm.simulate_fill(
            order_type=OrderType.MARKET,
            side=OrderSide.BUY,
            order_price=500.0,
            bar_open=500.0,
            bar_high=510.0,
            bar_low=495.0,
        )
        assert result.filled is True
        assert result.fill_price > 500.0  # buy fills above open (adverse)
        assert result.slippage_bps == pytest.approx(5.0, rel=0.1)

    def test_market_sell_subtracts_slippage(self):
        sm = SlippageModel()
        result = sm.simulate_fill(
            order_type=OrderType.MARKET,
            side=OrderSide.SELL,
            order_price=500.0,
            bar_open=500.0,
            bar_high=510.0,
            bar_low=495.0,
        )
        assert result.fill_price < 500.0  # sell fills below open (adverse)

    def test_large_quantity_increases_slippage(self):
        sm = SlippageModel()
        small = sm.simulate_fill(
            OrderType.MARKET,
            OrderSide.BUY,
            500.0,
            500.0,
            510.0,
            495.0,
            quantity=10,
            avg_daily_volume=1_000_000,
        )
        large = sm.simulate_fill(
            OrderType.MARKET,
            OrderSide.BUY,
            500.0,
            500.0,
            510.0,
            495.0,
            quantity=10_000,
            avg_daily_volume=1_000_000,
        )
        assert large.slippage_bps > small.slippage_bps

    def test_high_atr_increases_slippage(self):
        sm = SlippageModel()
        low_vol = sm.simulate_fill(
            OrderType.MARKET, OrderSide.BUY, 500.0, 500.0, 510.0, 495.0, atr_pct=0.01
        )
        high_vol = sm.simulate_fill(
            OrderType.MARKET, OrderSide.BUY, 500.0, 500.0, 510.0, 495.0, atr_pct=0.05
        )
        assert high_vol.slippage_bps > low_vol.slippage_bps


class TestSlippageLimitOrder:
    def test_limit_buy_fills_when_bar_low_crosses(self):
        sm = SlippageModel()
        result = sm.simulate_fill(
            OrderType.LIMIT,
            OrderSide.BUY,
            order_price=490.0,
            bar_open=500.0,
            bar_high=505.0,
            bar_low=488.0,
        )
        assert result.filled is True
        assert result.fill_price == pytest.approx(490.0)
        assert result.slippage_amount == pytest.approx(0.0)

    def test_limit_buy_does_not_fill_when_price_never_crosses(self):
        sm = SlippageModel()
        result = sm.simulate_fill(
            OrderType.LIMIT,
            OrderSide.BUY,
            order_price=480.0,
            bar_open=500.0,
            bar_high=505.0,
            bar_low=490.0,
        )
        assert result.filled is False

    def test_limit_sell_fills_when_bar_high_crosses(self):
        sm = SlippageModel()
        result = sm.simulate_fill(
            OrderType.LIMIT,
            OrderSide.SELL,
            order_price=510.0,
            bar_open=500.0,
            bar_high=515.0,
            bar_low=498.0,
        )
        assert result.filled is True
        assert result.fill_price == pytest.approx(510.0)


class TestSlippageStopOrder:
    def test_stop_sell_fills_when_bar_low_hits(self):
        sm = SlippageModel()
        result = sm.simulate_fill(
            OrderType.SL,
            OrderSide.SELL,
            order_price=490.0,
            bar_open=500.0,
            bar_high=505.0,
            bar_low=485.0,
        )
        assert result.filled is True
        # Stop fills with 1.5× base slippage adverse to stop direction
        assert result.fill_price < 490.0

    def test_stop_sell_does_not_fill_when_low_above_stop(self):
        sm = SlippageModel()
        result = sm.simulate_fill(
            OrderType.SL,
            OrderSide.SELL,
            order_price=490.0,
            bar_open=500.0,
            bar_high=505.0,
            bar_low=492.0,
        )
        assert result.filled is False

    def test_stop_slippage_factor_is_1_5x_base(self):
        sm = SlippageModel()
        market = sm.simulate_fill(OrderType.MARKET, OrderSide.BUY, 500.0, 500.0, 510.0, 495.0)
        stop = sm.simulate_fill(
            OrderType.SL,
            OrderSide.BUY,
            order_price=500.0,
            bar_open=500.0,
            bar_high=505.0,
            bar_low=498.0,
        )
        # Stop slippage should be 1.5× market slippage
        assert stop.slippage_bps == pytest.approx(market.slippage_bps * 1.5, rel=0.01)
