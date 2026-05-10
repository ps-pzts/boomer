from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from capital.models import RiskConfig, Track


class BreakerStatus(StrEnum):
    CLEAR = "clear"
    TRIPPED = "tripped"


@dataclass(frozen=True)
class CircuitBreakerState:
    """Snapshot of all circuit breaker statuses at a given moment.

    Computed from live trading data; not persisted directly.
    Persisting the trip/reset events is the job of CapitalStateManager.
    """

    intraday_daily_loss: BreakerStatus
    intraday_consecutive_losses: BreakerStatus
    intraday_late_entry: BreakerStatus  # time > 14:30 IST
    swing_weekly_loss: BreakerStatus
    swing_30d_loss_count: BreakerStatus
    portfolio_daily_loss: BreakerStatus
    portfolio_weekly_loss: BreakerStatus
    portfolio_max_drawdown: BreakerStatus  # manual resume required
    black_swan: BreakerStatus  # manual resume required

    def any_tripped(self) -> bool:
        return any(
            v == BreakerStatus.TRIPPED
            for v in self.__dataclass_fields__  # type: ignore[attr-defined]
        )

    def requires_manual_resume(self) -> bool:
        return (
            self.portfolio_max_drawdown == BreakerStatus.TRIPPED
            or self.black_swan == BreakerStatus.TRIPPED
        )

    def track_blocked(self, track: Track) -> bool:
        """True if this track cannot place new entries right now."""
        if self.portfolio_max_drawdown == BreakerStatus.TRIPPED:
            return True
        if self.portfolio_daily_loss == BreakerStatus.TRIPPED:
            return True
        if self.portfolio_weekly_loss == BreakerStatus.TRIPPED:
            return True
        if self.black_swan == BreakerStatus.TRIPPED:
            return True
        if track == Track.INTRADAY:
            return (
                self.intraday_daily_loss == BreakerStatus.TRIPPED
                or self.intraday_consecutive_losses == BreakerStatus.TRIPPED
                or self.intraday_late_entry == BreakerStatus.TRIPPED
            )
        if track == Track.SWING:
            return (
                self.swing_weekly_loss == BreakerStatus.TRIPPED
                or self.swing_30d_loss_count == BreakerStatus.TRIPPED
            )
        return False  # long_term has no daily/weekly breaker

    @classmethod
    def all_clear(cls) -> CircuitBreakerState:
        return cls(
            intraday_daily_loss=BreakerStatus.CLEAR,
            intraday_consecutive_losses=BreakerStatus.CLEAR,
            intraday_late_entry=BreakerStatus.CLEAR,
            swing_weekly_loss=BreakerStatus.CLEAR,
            swing_30d_loss_count=BreakerStatus.CLEAR,
            portfolio_daily_loss=BreakerStatus.CLEAR,
            portfolio_weekly_loss=BreakerStatus.CLEAR,
            portfolio_max_drawdown=BreakerStatus.CLEAR,
            black_swan=BreakerStatus.CLEAR,
        )


def evaluate_circuit_breakers(
    *,
    intraday_realised_pnl_today: Decimal,
    intraday_bucket_capital: Decimal,
    intraday_consecutive_losses_today: int,
    swing_realised_pnl_this_week: Decimal,
    swing_bucket_capital: Decimal,
    swing_losing_trades_30d: int,
    portfolio_realised_pnl_today: Decimal,
    total_capital: Decimal,
    portfolio_realised_pnl_this_week: Decimal,
    live_drawdown_pct: Decimal,
    nifty_intraday_move_pct: Decimal,
    current_time_ist_hour: int,
    current_time_ist_minute: int,
    black_swan_manually_tripped: bool,
    config: RiskConfig,
) -> CircuitBreakerState:
    """Pure function: compute all circuit breaker statuses from current trading data.

    All % comparisons use fractions (e.g., 0.02 = 2%), consistent with risk_config.
    """

    def _pct(pnl: Decimal, capital: Decimal) -> Decimal:
        if capital == 0:
            return Decimal("0")
        return pnl / capital

    intraday_loss_pct = _pct(-intraday_realised_pnl_today, intraday_bucket_capital)
    swing_loss_pct = _pct(-swing_realised_pnl_this_week, swing_bucket_capital)
    portfolio_daily_loss_pct = _pct(-portfolio_realised_pnl_today, total_capital)
    portfolio_weekly_loss_pct = _pct(-portfolio_realised_pnl_this_week, total_capital)

    is_after_1430 = (current_time_ist_hour > 14) or (
        current_time_ist_hour == 14 and current_time_ist_minute >= 30
    )

    return CircuitBreakerState(
        intraday_daily_loss=BreakerStatus.TRIPPED
        if intraday_loss_pct >= config.intraday_daily_loss_limit_pct
        else BreakerStatus.CLEAR,
        intraday_consecutive_losses=BreakerStatus.TRIPPED
        if intraday_consecutive_losses_today >= config.intraday_consecutive_loss_count
        else BreakerStatus.CLEAR,
        intraday_late_entry=BreakerStatus.TRIPPED if is_after_1430 else BreakerStatus.CLEAR,
        swing_weekly_loss=BreakerStatus.TRIPPED
        if swing_loss_pct >= config.swing_weekly_loss_limit_pct
        else BreakerStatus.CLEAR,
        swing_30d_loss_count=BreakerStatus.TRIPPED
        if swing_losing_trades_30d >= config.swing_30d_loss_count
        else BreakerStatus.CLEAR,
        portfolio_daily_loss=BreakerStatus.TRIPPED
        if portfolio_daily_loss_pct >= config.portfolio_daily_loss_limit_pct
        else BreakerStatus.CLEAR,
        portfolio_weekly_loss=BreakerStatus.TRIPPED
        if portfolio_weekly_loss_pct >= config.portfolio_weekly_loss_limit_pct
        else BreakerStatus.CLEAR,
        portfolio_max_drawdown=BreakerStatus.TRIPPED
        if live_drawdown_pct >= config.portfolio_max_drawdown_pct
        else BreakerStatus.CLEAR,
        black_swan=BreakerStatus.TRIPPED
        if (
            black_swan_manually_tripped
            or nifty_intraday_move_pct <= -config.nifty_intraday_pause_pct
        )
        else BreakerStatus.CLEAR,
    )
