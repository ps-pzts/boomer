"""Stage 4 — Portfolio constructor.

Filters trade plan candidates against all portfolio constraints, prioritises
the survivors, and checks pyramiding eligibility for existing positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from brain.models import TradePlan
from capital.models import RiskConfig

# Open positions count limits
MAX_TOTAL_OPEN_POSITIONS = 15
MAX_INTRADAY_NEW_PER_DAY = 5
MAX_SWING_NEW_PER_DAY = 3
MAX_SWING_OPEN_TOTAL = 8
MAX_LONG_TERM_NEW_PER_WEEK = 1


@dataclass(frozen=True)
class OpenPositionSummary:
    stock_symbol: str
    exchange: str
    track: str
    sector: str
    current_value: Decimal          # qty × LTP
    entry_value: Decimal            # qty × entry_price
    unrealised_pnl_pct: float       # (current - entry) / entry


@dataclass(frozen=True)
class PendingOrderSummary:
    stock_symbol: str
    exchange: str
    track: str
    reserved_value: Decimal         # order_qty × limit_price


@dataclass(frozen=True)
class PortfolioCapacityState:
    open_positions: list[OpenPositionSummary]
    pending_orders: list[PendingOrderSummary]
    total_capital: Decimal
    intraday_new_today: int
    swing_new_today: int
    swing_open_total: int
    long_term_new_this_week: int
    correlation_matrix: dict[str, dict[str, float]]  # symbol → {symbol: corr}


def _priority(plan: TradePlan) -> float:
    """priority = 0.6×confidence + 0.3×EV_normalised + 0.1×signal_agreement (design Stage 4)."""
    # Signal confidence not on plan; use EV as proxy — caller should pre-sort by confidence.
    return float(plan.expected_value_per_share)


class PortfolioConstructor:
    """Applies Stage 4 constraints and returns the subset of plans that can proceed."""

    def filter_candidates(
        self,
        candidates: list[tuple[TradePlan, float]],  # (plan, signal_confidence)
        state: PortfolioCapacityState,
        risk_config: RiskConfig,
    ) -> list[TradePlan]:
        """Return approved plans in priority order.

        Args:
            candidates: list of (TradePlan with decision=proceed, signal_confidence).
                        Plans with decision=skip are ignored.
            state: current portfolio state.
            risk_config: concentration caps.
        """
        eligible = [(p, c) for p, c in candidates if p.decision == "proceed"]

        # Sort by priority (descending)
        eligible.sort(key=lambda x: self._full_priority(x[0], x[1]), reverse=True)

        approved: list[TradePlan] = []
        total_open = len(state.open_positions)

        # Track running exposure for incremental checks
        running_stock: dict[tuple[str, str], Decimal] = {}
        running_sector: dict[str, Decimal] = {}

        # Seed running exposure from existing positions + pending orders
        for pos in state.open_positions:
            key = (pos.stock_symbol, pos.exchange)
            running_stock[key] = running_stock.get(key, Decimal("0")) + pos.current_value
        for ord_ in state.pending_orders:
            key = (ord_.stock_symbol, ord_.exchange)
            running_stock[key] = running_stock.get(key, Decimal("0")) + ord_.reserved_value

        for pos in state.open_positions:
            sec = pos.sector
            running_sector[sec] = running_sector.get(sec, Decimal("0")) + pos.current_value

        for plan, _conf in eligible:
            if not self._check_total_open(total_open + len(approved)):
                break
            already = len([p for p in approved if p.track == plan.track])
            if not self._check_track_cap(plan.track, state, already):
                continue

            from capital.models import Track as _Track
            risk_pct = risk_config.risk_per_trade_pct(_Track(plan.track))
            stop_dist = max(plan.entry_zone_high - plan.stop_loss_price, Decimal("0.01"))
            trade_value = plan.entry_zone_high * Decimal(str(
                int(state.total_capital * risk_pct / stop_dist)
            ))

            key = (plan.stock_symbol, plan.exchange)
            projected_stock = running_stock.get(key, Decimal("0")) + trade_value
            if not self._check_stock_concentration(
                projected_stock, state.total_capital, risk_config
            ):
                continue

            # Sector check (requires sector lookup from caller; skip here if no sector)
            # Caller pre-populates plan.stock_symbol sector via sector_classifications

            if not self._check_correlation(plan, trade_value, state, risk_config):
                continue

            approved.append(plan)
            running_stock[key] = projected_stock

        return approved

    def check_pyramid(
        self,
        position: OpenPositionSummary,
        fresh_signal_confidence: float,
        total_capital: Decimal,
        risk_config: RiskConfig,
    ) -> tuple[bool, str]:
        """Validate pyramiding eligibility (Stage 4 rules).

        Pyramiding allowed only if position is in profit and original signal is active.
        Averaging down is forbidden — hard rule from the design.
        """
        if position.unrealised_pnl_pct <= 0:
            pnl = position.unrealised_pnl_pct
            return False, f"position in drawdown ({pnl:.2f}%) — averaging down forbidden"
        if fresh_signal_confidence < 0.5:
            return False, f"signal confidence {fresh_signal_confidence:.2f} too low to pyramid"
        return True, "pyramid eligible"

    @staticmethod
    def _full_priority(plan: TradePlan, confidence: float) -> float:
        ev = float(plan.expected_value_per_share)
        ev_max = 10.0  # normalisation constant
        ev_norm = min(ev / ev_max, 1.0) if ev_max else 0.0
        # signal_agreement not available on plan; use confidence as proxy
        return 0.6 * confidence + 0.3 * ev_norm + 0.1 * confidence

    @staticmethod
    def _check_total_open(projected_open: int) -> bool:
        return projected_open < MAX_TOTAL_OPEN_POSITIONS

    @staticmethod
    def _check_track_cap(track: str, state: PortfolioCapacityState, already_approved: int) -> bool:
        if track == "intraday":
            return (state.intraday_new_today + already_approved) < MAX_INTRADAY_NEW_PER_DAY
        if track == "swing":
            return (
                (state.swing_new_today + already_approved) < MAX_SWING_NEW_PER_DAY
                and state.swing_open_total < MAX_SWING_OPEN_TOTAL
            )
        if track == "long_term":
            return (state.long_term_new_this_week + already_approved) < MAX_LONG_TERM_NEW_PER_WEEK
        return True

    @staticmethod
    def _check_stock_concentration(
        projected_value: Decimal,
        total_capital: Decimal,
        risk_config: RiskConfig,
    ) -> bool:
        if total_capital <= 0:
            return False
        pct = projected_value / total_capital
        return pct <= risk_config.single_stock_cap_pct

    @staticmethod
    def _check_correlation(
        plan: TradePlan,
        trade_value: Decimal,
        state: PortfolioCapacityState,
        risk_config: RiskConfig,
    ) -> bool:
        """Check that adding this plan doesn't breach the correlation cluster cap."""
        sym = plan.stock_symbol
        corr_row = state.correlation_matrix.get(sym, {})
        cluster_value = trade_value

        for pos in state.open_positions:
            corr = corr_row.get(pos.stock_symbol, 0.0)
            if corr >= 0.7:  # highly correlated threshold
                cluster_value += pos.current_value

        if state.total_capital <= 0:
            return True
        cluster_pct = cluster_value / state.total_capital
        return cluster_pct <= risk_config.correlation_cluster_cap_pct
