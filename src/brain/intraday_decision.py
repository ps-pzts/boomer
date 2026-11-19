"""One symbol, one moment: enter or not, and exactly how (#69, design §3.5 steps 3-5).

Pure apart from the generators it composes; no DB, no broker, no clock. Returns
an `EntryDecision`, or a `Rejection` naming the rule that fired and its inputs
(CLAUDE.md §9 attribution).

Flow:
  signal (IntradaySignalGenerator) -> must be LONG (v1)
  plan at the live price (TradePlanGenerator): quality/liquidity/RR/EV gates
  entry strategy + price (IntradayClassifier)
  plan again AT THE ENTRY PRICE: stop, target, RR, EV are for the price we'll pay
  size (capital.sizing: risk-based, shrunk to the single-stock cap)

Entry orders (operator decision 2026-11-19):
  ID1 ORB  -> stop-limit BUY: trigger = ORB high +0.1%, limit = trigger +0.2%.
              Already above the trigger but within the limit -> LIMIT at the limit.
              Above the limit -> rejected (`breakout_extended`): never chase.
  ID2/ID3  -> LIMIT at VWAP (waits for the pullback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from brain.entry_timing import IntradayClassifier
from brain.models import Direction, EntryStrategy, SignalRecord, TradePlan
from brain.signals.intraday import IntradaySignalGenerator
from brain.trade_decision import TradePlanGenerator
from capital.models import REGIME_EXPOSURE_SCALE, CapitalLedgerRow, Regime, RiskConfig, Track
from capital.sizing import Sizing, intraday_quantity

TICK = Decimal("0.05")
ORB_LIMIT_BAND = Decimal("1.002")  # limit = trigger + 0.2%


@dataclass(frozen=True)
class Rejection:
    symbol: str
    rule: str
    inputs: dict[str, Any] = field(default_factory=dict)
    signal: SignalRecord | None = None
    plan: TradePlan | None = None


@dataclass(frozen=True)
class EntryDecision:
    symbol: str
    exchange: str
    strategy: EntryStrategy
    entry_price: Decimal  # the order's LIMIT price (worst fill)
    entry_trigger: Decimal | None  # set for a stop-limit (ORB) entry
    stop_price: Decimal
    target_price: Decimal
    atr: Decimal
    sizing: Sizing
    signal: SignalRecord
    plan: TradePlan

    @property
    def quantity(self) -> int:
        return self.sizing.quantity

    def as_inputs(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "entry_price": str(self.entry_price),
            "entry_trigger": str(self.entry_trigger) if self.entry_trigger else None,
            "stop": str(self.stop_price),
            "target": str(self.target_price),
            "quantity": self.quantity,
            "sizing_binding": self.sizing.binding,
            "risk_rupees": str(self.sizing.risk_rupees),
            "raw_score": self.signal.raw_score,
            "confidence": self.signal.confidence,
            "rr": str(self.plan.reward_to_risk),
        }


def _tick_up(x: Decimal) -> Decimal:
    return (x / TICK).to_integral_value(rounding=ROUND_CEILING) * TICK


def decide(
    *,
    symbol: str,
    exchange: str,
    features: dict[str, Any],
    ltp: float | None,
    regime: str,
    now: datetime,
    config: RiskConfig,
    ledger: CapitalLedgerRow,
) -> EntryDecision | Rejection:
    if ltp is None or ltp <= 0:
        return Rejection(symbol, "no_live_price")
    atr_raw = features.get("atr_14d")
    if not atr_raw or float(atr_raw) <= 0:
        return Rejection(symbol, "atr_unavailable")
    price, atr = Decimal(str(ltp)), Decimal(str(atr_raw))

    signal = IntradaySignalGenerator().generate(symbol, exchange, features, regime, now)
    if signal is None:
        return Rejection(symbol, "signal_insufficient_data")
    if signal.direction != Direction.LONG:
        return Rejection(symbol, "signal_not_long", {"raw_score": signal.raw_score}, signal=signal)

    bucket = ledger.bucket_capital(Track.INTRADAY)
    plans = TradePlanGenerator()
    gate = plans.generate(signal, price, atr, bucket, config, now)
    if gate.decision != "proceed":
        return _plan_rejection(symbol, gate, signal, price)

    entries = IntradayClassifier().classify(gate, features)
    if not entries:
        return Rejection(
            symbol,
            "no_entry_strategy",
            {k: features.get(k) for k in ("premarket_gap_pct", "vwap_current", "orb_high")}
            | {"minutes_since_market_open": features.get("minutes_since_market_open")},
            signal=signal,
        )
    ep = entries[0]

    trigger: Decimal | None = None
    entry = ep.entry_price
    if ep.strategy == EntryStrategy.ID1:
        limit = _tick_up(ep.entry_price * ORB_LIMIT_BAND)
        if price >= limit:
            return Rejection(
                symbol,
                "breakout_extended",
                {"ltp": str(price), "trigger": str(ep.entry_price), "limit": str(limit)},
                signal=signal,
            )
        trigger = ep.entry_price if price < ep.entry_price else None
        entry = limit

    plan = plans.generate(signal, entry, atr, bucket, config, now)
    if plan.decision != "proceed":
        return _plan_rejection(symbol, plan, signal, entry)
    if trigger is not None and not plan.stop_loss_price < trigger:
        return Rejection(symbol, "stop_above_trigger", {"trigger": str(trigger)}, signal, plan)

    sizing = intraday_quantity(
        bucket_capital=bucket,
        total_capital=ledger.total_capital,
        risk_pct=config.risk_per_trade_pct(Track.INTRADAY),
        single_stock_cap_pct=config.single_stock_cap_pct,
        regime_scale=REGIME_EXPOSURE_SCALE[Regime(regime)],
        entry=entry,
        stop=plan.stop_loss_price,
    )
    if sizing.quantity < 1:
        return Rejection(
            symbol,
            "position_too_small",
            {"risk_shares": sizing.risk_shares, "cap_shares": sizing.cap_shares},
            signal,
            plan,
        )
    return EntryDecision(
        symbol=symbol,
        exchange=exchange,
        strategy=ep.strategy,
        entry_price=entry,
        entry_trigger=trigger,
        stop_price=plan.stop_loss_price,
        target_price=plan.target_price,
        atr=atr,
        sizing=sizing,
        signal=signal,
        plan=plan,
    )


def _plan_rejection(
    symbol: str, plan: TradePlan, signal: SignalRecord, price: Decimal
) -> Rejection:
    reason = plan.skip_reason.value if plan.skip_reason else "unknown"
    return Rejection(
        symbol,
        f"plan_{reason}",
        {"price": str(price), "confidence": signal.confidence},
        signal=signal,
        plan=plan,
    )
