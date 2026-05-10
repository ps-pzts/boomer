from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from brain.models import Direction, SignalRecord, SkipReason, TradePlan
from capital.models import ATR_K, MIN_RR, RiskConfig, Track

# Round-trip cost estimate: brokerage + STT + exchange + GST (basis points)
ROUND_TRIP_COST_BPS = Decimal("30")  # 0.30%

# Minimum target distance as fraction of ATR (target-too-far check)
MIN_TARGET_ATR_FRACTION = Decimal("0.5")


class TradePlanGenerator:
    """Stage 3 — converts a signal into a concrete trade plan with full validation.

    Decision flow (7 steps from design doc):
      1. Compute entry zone (placeholder; refined by Stage 3.5)
      2. ATR-based stop
      3. ATR/RR-based target
      4. RR gate
      5. EV gate with confidence haircut
      6. Position feasibility
      7. Target-too-close check
    """

    def generate(
        self,
        signal: SignalRecord,
        current_price: Decimal,
        atr_14d: Decimal,
        bucket_capital: Decimal,
        risk_config: RiskConfig,
        generated_at: datetime,
    ) -> TradePlan:
        """Return a TradePlan with decision=proceed or decision=skip+reason."""
        plan_id = str(uuid.uuid4())
        track = Track(signal.track)

        if signal.direction != Direction.LONG:
            return self._skip(
                plan_id, signal, current_price, generated_at,
                SkipReason.DATA_UNAVAILABLE, "non-long direction not supported in v1",
            )

        # Step 1: entry zone (±0.5% of current price; refined by Stage 3.5)
        entry_low = current_price * Decimal("0.995")
        entry_high = current_price * Decimal("1.005")
        entry_mid = current_price

        # Step 2: ATR-based stop
        k = ATR_K[track]
        stop_price = (entry_mid - k * atr_14d).quantize(Decimal("0.05"))

        # Step 3: target
        rr_min = MIN_RR[track]
        stop_distance = entry_mid - stop_price
        if stop_distance <= 0:
            return self._skip(
                plan_id, signal, current_price, generated_at,
                SkipReason.DATA_UNAVAILABLE, "stop_distance non-positive",
            )

        target_price = (entry_mid + rr_min * stop_distance).quantize(Decimal("0.05"))

        # Step 4: RR gate
        reward = target_price - entry_mid
        risk = stop_distance
        rr_actual = reward / risk if risk > 0 else Decimal("0")

        if rr_actual < rr_min:
            return self._skip(
                plan_id, signal, current_price, generated_at, SkipReason.RR_TOO_LOW,
                f"RR {rr_actual:.2f} < minimum {rr_min}"
            )

        # Step 5: EV gate with haircut
        haircut = risk_config.live_backtest_ratio(track)
        p_win = Decimal(str(signal.confidence)) * haircut
        p_loss = Decimal("1") - p_win
        cost = entry_mid * ROUND_TRIP_COST_BPS / Decimal("10000")
        reward_after_costs = reward - cost
        risk_after_costs = risk + cost
        ev = p_win * reward_after_costs - p_loss * risk_after_costs

        if ev <= 0:
            msg = (
                f"EV {ev:.4f} ≤ 0 "
                f"(p_win={p_win:.3f} rwd={reward_after_costs:.2f} rsk={risk_after_costs:.2f})"
            )
            return self._skip(
                plan_id, signal, current_price, generated_at, SkipReason.EV_NEGATIVE, msg
            )

        # Step 6: position feasibility
        risk_pct = risk_config.risk_per_trade_pct(track)
        risk_rupees = bucket_capital * risk_pct
        shares = int((risk_rupees / risk).to_integral_value(rounding=ROUND_DOWN))

        if shares < 1:
            return self._skip(
                plan_id, signal, current_price, generated_at, SkipReason.POSITION_TOO_SMALL,
                f"shares={shares} from risk_rupees={risk_rupees:.0f} / risk={risk:.2f}"
            )

        # Step 7: target-too-close check (must be at least 0.5 × ATR from entry)
        if reward < MIN_TARGET_ATR_FRACTION * atr_14d:
            min_dist = MIN_TARGET_ATR_FRACTION * atr_14d
            msg = f"target only {reward:.2f} away, need ≥ {min_dist:.2f} (0.5×ATR)"
            return self._skip(
                plan_id, signal, current_price, generated_at, SkipReason.TARGET_TOO_CLOSE, msg
            )

        return TradePlan(
            plan_id=plan_id,
            signal_id=signal.signal_id,
            stock_symbol=signal.stock_symbol,
            exchange=signal.exchange,
            track=signal.track,
            direction=signal.direction,
            entry_zone_low=entry_low.quantize(Decimal("0.05")),
            entry_zone_high=entry_high.quantize(Decimal("0.05")),
            stop_loss_price=stop_price,
            target_price=target_price,
            expected_reward_per_share=reward,
            expected_risk_per_share=risk,
            reward_to_risk=rr_actual,
            expected_value_per_share=ev,
            decision="proceed",
            skip_reason=None,
            entry_strategy_id=None,  # assigned by Stage 3.5
            created_at=generated_at,
        )

    def _skip(
        self,
        plan_id: str,
        signal: SignalRecord,
        current_price: Decimal,
        generated_at: datetime,
        reason: SkipReason,
        detail: str,
    ) -> TradePlan:
        zero = Decimal("0")
        return TradePlan(
            plan_id=plan_id,
            signal_id=signal.signal_id,
            stock_symbol=signal.stock_symbol,
            exchange=signal.exchange,
            track=signal.track,
            direction=signal.direction,
            entry_zone_low=current_price,
            entry_zone_high=current_price,
            stop_loss_price=zero,
            target_price=zero,
            expected_reward_per_share=zero,
            expected_risk_per_share=zero,
            reward_to_risk=zero,
            expected_value_per_share=zero,
            decision="skip",
            skip_reason=reason,
            entry_strategy_id=None,
            created_at=generated_at,
        )
