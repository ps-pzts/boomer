from __future__ import annotations

from dataclasses import dataclass

from executor.models import OrderSide, OrderType


@dataclass(frozen=True)
class SlippageResult:
    fill_price: float
    slippage_amount: float   # absolute ₹ per share
    slippage_bps: float      # basis points of reference price
    filled: bool             # False if limit order didn't cross


class SlippageModel:
    """
    Realistic fill simulation for backtesting.

    Per Phase 4 design doc — conservative numbers; live results may be
    better, never assume so during backtest.

    Limit orders fill only if price crosses the limit level during the bar.
    Stop orders use 1.5× base slippage (fills worse in fast markets — Loophole 2).
    Market orders: base 5 bps × liquidity × volatility adjustments.

    Worked example — market buy at ₹500 on a liquid stock:
        base_slippage = 0.05% = ₹0.25
        liquidity_adj = 1.0 (quantity << 0.1% ADV)
        volatility_adj = 1.0 (ATR 2%)
        fill_price = 500 + 0.25 = ₹500.25
        slippage_bps = 0.25/500 × 10000 = 5.0 bps ✓
    """

    BASE_SLIPPAGE_PCT = 0.0005      # 5 bps base for market orders
    STOP_SLIPPAGE_FACTOR = 1.5      # stops fill worse in fast markets
    ADV_IMPACT_THRESHOLD = 0.001    # 0.1% of ADV — above this, liquidity penalty kicks in
    ATR_NORMAL_PCT = 0.02           # 2% ATR reference

    def simulate_fill(
        self,
        order_type: OrderType,
        side: OrderSide,
        order_price: float,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        quantity: int = 1,
        avg_daily_volume: int = 1_000_000,
        atr_pct: float = 0.02,
    ) -> SlippageResult:
        """
        Simulate order fill against a single OHLCV bar.

        Returns SlippageResult with filled=False if limit order doesn't cross.
        """
        if order_type == OrderType.MARKET:
            return self._market_fill(side, bar_open, quantity, avg_daily_volume, atr_pct)

        if order_type in (OrderType.SL, OrderType.SL_LIMIT):
            return self._stop_fill(side, order_price, bar_low, bar_high, atr_pct)

        if order_type == OrderType.LIMIT:
            return self._limit_fill(side, order_price, bar_low, bar_high)

        return SlippageResult(
            fill_price=bar_open, slippage_amount=0.0, slippage_bps=0.0, filled=True
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _market_fill(
        self,
        side: OrderSide,
        bar_open: float,
        quantity: int,
        avg_daily_volume: int,
        atr_pct: float,
    ) -> SlippageResult:
        liquidity_adj = max(1.0, quantity / max(1, avg_daily_volume * self.ADV_IMPACT_THRESHOLD))
        volatility_adj = max(1.0, atr_pct / self.ATR_NORMAL_PCT)
        slippage_pct = self.BASE_SLIPPAGE_PCT * liquidity_adj * volatility_adj
        slippage_amount = bar_open * slippage_pct
        if side == OrderSide.BUY:
            fill_price = bar_open + slippage_amount
        else:
            fill_price = bar_open - slippage_amount
        return SlippageResult(
            fill_price=round(fill_price, 2),
            slippage_amount=round(slippage_amount, 4),
            slippage_bps=round(slippage_pct * 10_000, 2),
            filled=True,
        )

    def _stop_fill(
        self,
        side: OrderSide,
        stop_price: float,
        bar_low: float,
        bar_high: float,
        atr_pct: float,
    ) -> SlippageResult:
        if side == OrderSide.SELL and bar_low > stop_price:
            return SlippageResult(
                fill_price=stop_price, slippage_amount=0.0, slippage_bps=0.0, filled=False
            )
        if side == OrderSide.BUY and bar_high < stop_price:
            return SlippageResult(
                fill_price=stop_price, slippage_amount=0.0, slippage_bps=0.0, filled=False
            )

        slippage_pct = self.BASE_SLIPPAGE_PCT * self.STOP_SLIPPAGE_FACTOR
        slippage_amount = stop_price * slippage_pct
        if side == OrderSide.SELL:
            fill_price = stop_price - slippage_amount  # gap-down fills worse
        else:
            fill_price = stop_price + slippage_amount
        return SlippageResult(
            fill_price=round(fill_price, 2),
            slippage_amount=round(slippage_amount, 4),
            slippage_bps=round(slippage_pct * 10_000, 2),
            filled=True,
        )

    def _limit_fill(
        self,
        side: OrderSide,
        limit_price: float,
        bar_low: float,
        bar_high: float,
    ) -> SlippageResult:
        if side == OrderSide.BUY and bar_low <= limit_price:
            return SlippageResult(
                fill_price=limit_price, slippage_amount=0.0, slippage_bps=0.0, filled=True
            )
        if side == OrderSide.SELL and bar_high >= limit_price:
            return SlippageResult(
                fill_price=limit_price, slippage_amount=0.0, slippage_bps=0.0, filled=True
            )
        return SlippageResult(
            fill_price=limit_price, slippage_amount=0.0, slippage_bps=0.0, filled=False
        )
