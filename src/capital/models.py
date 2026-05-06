from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class Track(StrEnum):
    LONG_TERM = "long_term"
    SWING = "swing"
    INTRADAY = "intraday"


class Regime(StrEnum):
    BULL_CALM = "bull_calm"
    BULL_VOLATILE = "bull_volatile"
    SIDEWAYS = "sideways"
    BEAR = "bear"


class BotMode(StrEnum):
    AUTO = "auto"
    PAUSED = "paused"
    EMERGENCY_STOP = "emergency_stop"


# Q1-1 resolved: regime scaling applies to NEW ENTRIES only, not existing positions.
# Existing positions are protected by circuit breakers, not forced liquidation on regime shift.
REGIME_EXPOSURE_SCALE: dict[Regime, Decimal] = {
    Regime.BULL_CALM: Decimal("1.00"),
    Regime.BULL_VOLATILE: Decimal("0.70"),
    Regime.SIDEWAYS: Decimal("0.50"),
    Regime.BEAR: Decimal("0.30"),
}

# Capital milestone that triggers the allocation shift from initial to steady-state.
CAPITAL_MILESTONE = Decimal("250000")  # ₹2,50,000

INITIAL_ALLOCATION: dict[Track, Decimal] = {
    Track.LONG_TERM: Decimal("0.80"),
    Track.SWING: Decimal("0.15"),
    Track.INTRADAY: Decimal("0.05"),
}

STEADY_ALLOCATION: dict[Track, Decimal] = {
    Track.LONG_TERM: Decimal("0.70"),
    Track.SWING: Decimal("0.15"),
    Track.INTRADAY: Decimal("0.15"),
}


def allocation_for_capital(total_capital: Decimal) -> dict[Track, Decimal]:
    """Return the correct allocation dict given current total capital."""
    if total_capital >= CAPITAL_MILESTONE:
        return STEADY_ALLOCATION
    return INITIAL_ALLOCATION


# RR minimums per track
MIN_RR: dict[Track, Decimal] = {
    Track.INTRADAY: Decimal("1.5"),
    Track.SWING: Decimal("1.5"),
    Track.LONG_TERM: Decimal("2.0"),
}

# ATR multiplier for stop placement per track
ATR_K: dict[Track, Decimal] = {
    Track.INTRADAY: Decimal("1.5"),
    Track.SWING: Decimal("2.0"),
    Track.LONG_TERM: Decimal("3.0"),
}


@dataclass(frozen=True)
class RiskConfig:
    config_id: str
    version: int
    effective_from: date
    # Position sizing (fraction of bucket capital)
    risk_per_intraday_trade_pct: Decimal
    risk_per_swing_trade_pct: Decimal
    risk_per_long_term_trade_pct: Decimal
    # Track-level circuit breakers
    intraday_daily_loss_limit_pct: Decimal
    swing_weekly_loss_limit_pct: Decimal
    # Portfolio-level circuit breakers
    portfolio_daily_loss_limit_pct: Decimal
    portfolio_weekly_loss_limit_pct: Decimal
    portfolio_max_drawdown_pct: Decimal
    # Concentration caps (all as fraction of TOTAL capital)
    single_stock_cap_pct: Decimal
    sector_cap_pct: Decimal
    correlation_cluster_cap_pct: Decimal
    # Track decay triggers
    intraday_consecutive_loss_count: int
    swing_30d_loss_count: int
    # Black swan
    nifty_intraday_pause_pct: Decimal
    # Per-track confidence haircut (Q3-4: stored here, recalibrated after 60 live trades per track)
    live_backtest_ratio_long_term: Decimal
    live_backtest_ratio_swing: Decimal
    live_backtest_ratio_intraday: Decimal
    # FinBERT confidence gate
    sentiment_confidence_threshold: Decimal

    def risk_per_trade_pct(self, track: Track) -> Decimal:
        return {
            Track.INTRADAY: self.risk_per_intraday_trade_pct,
            Track.SWING: self.risk_per_swing_trade_pct,
            Track.LONG_TERM: self.risk_per_long_term_trade_pct,
        }[track]

    def live_backtest_ratio(self, track: Track) -> Decimal:
        return {
            Track.INTRADAY: self.live_backtest_ratio_intraday,
            Track.SWING: self.live_backtest_ratio_swing,
            Track.LONG_TERM: self.live_backtest_ratio_long_term,
        }[track]


@dataclass(frozen=True)
class CapitalLedgerRow:
    ledger_id: str
    as_of_date: date
    total_capital: Decimal
    total_cash: Decimal
    long_term_allocated_pct: Decimal
    swing_allocated_pct: Decimal
    intraday_allocated_pct: Decimal
    long_term_deployed: Decimal
    swing_deployed: Decimal
    intraday_deployed: Decimal
    high_water_mark: Decimal
    eod_drawdown_pct: Decimal
    consecutive_loss_days: int
    peak_date: date

    def bucket_capital(self, track: Track) -> Decimal:
        return self.total_capital * self._allocated_pct(track)

    def bucket_deployed(self, track: Track) -> Decimal:
        return {
            Track.LONG_TERM: self.long_term_deployed,
            Track.SWING: self.swing_deployed,
            Track.INTRADAY: self.intraday_deployed,
        }[track]

    def bucket_available(self, track: Track) -> Decimal:
        return self.bucket_capital(track) - self.bucket_deployed(track)

    def _allocated_pct(self, track: Track) -> Decimal:
        return {
            Track.LONG_TERM: self.long_term_allocated_pct,
            Track.SWING: self.swing_allocated_pct,
            Track.INTRADAY: self.intraday_allocated_pct,
        }[track]


@dataclass(frozen=True)
class LiveCapitalView:
    """Computed on-demand from cash + open positions × LTP. Never persisted."""
    total_cash: Decimal
    open_position_value: Decimal       # sum(qty × LTP) for all open positions
    hwm: Decimal
    intraday_realised_pnl_today: Decimal
    intraday_unrealised_pnl: Decimal   # sum((LTP - entry) × qty) for open intraday

    @property
    def live_total_capital(self) -> Decimal:
        return self.total_cash + self.open_position_value

    @property
    def live_drawdown_pct(self) -> Decimal:
        if self.hwm == 0:
            return Decimal("0")
        return (self.hwm - self.live_total_capital) / self.hwm

    @property
    def live_intraday_pnl(self) -> Decimal:
        return self.intraday_realised_pnl_today + self.intraday_unrealised_pnl


class LTPSource(Protocol):
    """Abstraction for current market prices. Injected into capital checks."""

    def get_ltp(self, symbol: str, exchange: str) -> Decimal | None:
        """Return last traded price, or None if unavailable."""
        ...


@dataclass(frozen=True)
class TradeRequest:
    stock_symbol: str
    exchange: str
    track: Track
    entry_price: Decimal
    stop_loss_price: Decimal
    target_price: Decimal
    signal_confidence: Decimal    # 0.0–1.0
    sector: str
    current_regime: Regime
    requested_at: datetime         # UTC
    # Concentration inputs: existing holding + all pending resting orders for same stock
    existing_position_value: Decimal = Decimal("0")
    pending_order_value: Decimal = Decimal("0")


@dataclass(frozen=True)
class TradePermission:
    approved: bool
    position_size_shares: int
    risk_per_trade_rupees: Decimal
    failed_check: str | None
    reason: str | None

    @classmethod
    def approve(cls, position_size_shares: int, risk_per_trade_rupees: Decimal) -> TradePermission:
        return cls(
            approved=True,
            position_size_shares=position_size_shares,
            risk_per_trade_rupees=risk_per_trade_rupees,
            failed_check=None,
            reason=None,
        )

    @classmethod
    def reject(cls, check: str, reason: str) -> TradePermission:
        return cls(
            approved=False,
            position_size_shares=0,
            risk_per_trade_rupees=Decimal("0"),
            failed_check=check,
            reason=reason,
        )
