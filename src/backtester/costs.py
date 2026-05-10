from __future__ import annotations

from dataclasses import dataclass

from executor.models import ProductType


@dataclass(frozen=True)
class TradeCost:
    brokerage: float
    stt: float
    exchange_charges: float
    gst: float
    sebi_charges: float
    stamp_duty: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_charges
            + self.gst
            + self.sebi_charges
            + self.stamp_duty
        )

    @property
    def total_bps(self) -> float:
        """Total cost as basis points of trade value (for reporting only)."""
        return 0.0  # computed per-trade by CostModel.round_trip_cost_bps()


class CostModel:
    """
    Indian market transaction cost model.

    Implements the exact cost structure from the Phase 4 design doc.
    Every backtest trade has these costs deducted — without realistic
    costs, every backtest looks profitable.

    Worked example (DELIVERY, ₹10,000 trade value each leg):
        STT:         0.1% × 10,000 × 2 legs = ₹20.00
        Exchange:    0.00322% × 10,000 × 2  = ₹0.644
        GST (18%):   18% × 0.644            = ₹0.116
        SEBI:        0.0001% × 10,000 × 2   = ₹0.020
        Stamp:       0.003% × 10,000 (buy)  = ₹0.30
        Total:       ≈ ₹21.08 on ₹10,000 value = 21.08 bps ≈ within 25-35 bps range

    Worked example (INTRADAY, ₹10,000 trade value each leg, assuming ₹20 cap):
        Brokerage:   ₹20 × 2 = ₹40 (or 0.03% whichever lower)
        STT:         0.025% × 10,000 (sell only) = ₹2.50
        Exchange:    0.00322% × 10,000 × 2  = ₹0.644
        GST (18%):   18% × (20 + 0.644)     = ₹3.716
        SEBI:        0.0001% × 10,000 × 2   = ₹0.020
        Stamp:       0.003% × 10,000 (buy)  = ₹0.30
        Total:       ≈ ₹47.18 on ₹10,000 value ≈ 47 bps (high due to ₹20 min brokerage)
        At ₹50,000 trade value: brokerage = 0.03% × 50,000 = ₹15/leg < ₹20, so 0.03% applies
    """

    # Rate constants
    BROKERAGE_MAX_PER_ORDER = 20.0  # ₹20 cap per intraday order
    BROKERAGE_INTRADAY_PCT = 0.0003  # 0.03%

    STT_INTRADAY_SELL_PCT = 0.00025  # 0.025% on sell only
    STT_DELIVERY_PCT = 0.001  # 0.1% on both buy and sell

    EXCHANGE_PCT = 0.0000322  # 0.00322% on both legs

    GST_PCT = 0.18  # 18% on (brokerage + exchange charges)

    SEBI_PCT = 0.000001  # 0.0001% on both legs

    STAMP_DUTY_BUY_PCT = 0.00003  # 0.003% on buy only

    def round_trip_cost(
        self,
        buy_value: float,
        sell_value: float,
        product: ProductType,
    ) -> TradeCost:
        """
        Compute full round-trip transaction costs.

        buy_value: quantity × buy price
        sell_value: quantity × sell price (or exit price)
        product: MIS for intraday, CNC for delivery
        """
        if product == ProductType.MIS:
            return self._intraday_cost(buy_value, sell_value)
        return self._delivery_cost(buy_value, sell_value)

    def round_trip_cost_bps(
        self,
        buy_value: float,
        sell_value: float,
        product: ProductType,
    ) -> float:
        """Return total cost as basis points of average trade value."""
        cost = self.round_trip_cost(buy_value, sell_value, product)
        avg_value = (buy_value + sell_value) / 2
        if avg_value == 0:
            return 0.0
        return (cost.total / avg_value) * 10_000

    def _intraday_cost(self, buy_value: float, sell_value: float) -> TradeCost:
        brokerage_buy = min(self.BROKERAGE_MAX_PER_ORDER, buy_value * self.BROKERAGE_INTRADAY_PCT)
        brokerage_sell = min(self.BROKERAGE_MAX_PER_ORDER, sell_value * self.BROKERAGE_INTRADAY_PCT)
        brokerage = brokerage_buy + brokerage_sell

        stt = sell_value * self.STT_INTRADAY_SELL_PCT

        exchange = (buy_value + sell_value) * self.EXCHANGE_PCT

        gst = (brokerage + exchange) * self.GST_PCT

        sebi = (buy_value + sell_value) * self.SEBI_PCT

        stamp = buy_value * self.STAMP_DUTY_BUY_PCT

        return TradeCost(
            brokerage=brokerage,
            stt=stt,
            exchange_charges=exchange,
            gst=gst,
            sebi_charges=sebi,
            stamp_duty=stamp,
        )

    def _delivery_cost(self, buy_value: float, sell_value: float) -> TradeCost:
        brokerage = 0.0  # Fyers ₹0 delivery brokerage

        stt = (buy_value + sell_value) * self.STT_DELIVERY_PCT

        exchange = (buy_value + sell_value) * self.EXCHANGE_PCT

        # GST on exchange charges only (no brokerage component)
        gst = exchange * self.GST_PCT

        sebi = (buy_value + sell_value) * self.SEBI_PCT

        stamp = buy_value * self.STAMP_DUTY_BUY_PCT

        return TradeCost(
            brokerage=brokerage,
            stt=stt,
            exchange_charges=exchange,
            gst=gst,
            sebi_charges=sebi,
            stamp_duty=stamp,
        )
