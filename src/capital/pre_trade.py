from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from capital.circuit_breakers import CircuitBreakerState
from capital.models import (
    MIN_RR,
    REGIME_EXPOSURE_SCALE,
    CapitalLedgerRow,
    RiskConfig,
    Track,
    TradePermission,
    TradeRequest,
)


class ConcentrationSource(Protocol):
    """Provides sector and correlation data. Injected to keep capital module decoupled."""

    def sector_deployed(self, sector: str) -> Decimal:
        """Current ₹ value deployed across all tracks in this sector."""
        ...

    def correlation_cluster_deployed(self, symbol: str, exchange: str) -> Decimal:
        """Current ₹ value of the correlation cluster containing this symbol."""
        ...


class PreTradeChecker:
    """Runs the 8-step pre-trade check from Phase 1 design, cheap checks first.

    Each check returns a TradePermission.reject(...) on failure;
    all checks passing returns TradePermission.approve(...) with computed position size.
    """

    def __init__(
        self,
        config: RiskConfig,
        breakers: CircuitBreakerState,
        ledger: CapitalLedgerRow,
        concentration: ConcentrationSource,
        live_total_capital: Decimal,
        live_drawdown_pct: Decimal,
        live_intraday_pnl: Decimal,
        bot_mode: str,
    ) -> None:
        self._config = config
        self._breakers = breakers
        self._ledger = ledger
        self._concentration = concentration
        self._live_total_capital = live_total_capital
        self._live_drawdown_pct = live_drawdown_pct
        self._live_intraday_pnl = live_intraday_pnl
        self._bot_mode = bot_mode

    def check(self, req: TradeRequest) -> TradePermission:
        """Run all checks in order. Returns at first failure."""
        # Time checks (market open, 14:30 cutoff) are encoded in circuit_breakers.py
        # and fire via _check_track_cooldown. No separate time check needed here.
        checks = [
            self._check_bot_state,
            self._check_track_cooldown,
            self._check_regime_gate,
            self._check_black_swan,
            self._check_capital_availability,
            self._check_concentration,
            self._check_trade_quality,
        ]
        for fn in checks:
            result = fn(req)
            if result is not None:
                return result

        return self._compute_approval(req)

    # ------------------------------------------------------------------
    # Check 1: Bot in shutdown state
    # ------------------------------------------------------------------

    def _check_bot_state(self, req: TradeRequest) -> TradePermission | None:
        if self._bot_mode == "emergency_stop":
            return TradePermission.reject("bot_state", "bot_mode=emergency_stop")
        if self._breakers.portfolio_max_drawdown.value == "tripped":
            return TradePermission.reject(
                "bot_state", "portfolio drawdown >= 8% — manual resume required"
            )
        return None

    # ------------------------------------------------------------------
    # Check 2: Track cooldown (delegated to caller via breaker state)
    # ------------------------------------------------------------------

    def _check_track_cooldown(self, req: TradeRequest) -> TradePermission | None:
        if self._breakers.track_blocked(req.track):
            return TradePermission.reject(
                "track_cooldown", f"circuit breaker active for track={req.track.value}"
            )
        return None

    # ------------------------------------------------------------------
    # Check 3: Regime gate (Q1-1: applies to new entries only)
    # ------------------------------------------------------------------

    def _check_regime_gate(self, req: TradeRequest) -> TradePermission | None:
        scale = REGIME_EXPOSURE_SCALE[req.current_regime]
        bucket_capital = self._ledger.bucket_capital(req.track)
        allowed_deployed = bucket_capital * scale
        deployed = self._ledger.bucket_deployed(req.track)
        if deployed >= allowed_deployed:
            return TradePermission.reject(
                "regime_gate",
                f"regime={req.current_regime.value} caps {req.track.value} deployment at "
                f"{scale * 100:.0f}% of bucket; already at limit",
            )
        return None

    # ------------------------------------------------------------------
    # Check 4: Black swan
    # ------------------------------------------------------------------

    def _check_black_swan(self, req: TradeRequest) -> TradePermission | None:
        if self._breakers.black_swan.value == "tripped":
            return TradePermission.reject("black_swan", "Nifty black-swan move — manual resume")
        return None

    # ------------------------------------------------------------------
    # Check 5: Capital availability in bucket
    # ------------------------------------------------------------------

    def _check_capital_availability(self, req: TradeRequest) -> TradePermission | None:
        available = self._ledger.bucket_available(req.track)
        if available <= 0:
            return TradePermission.reject(
                "capital_availability",
                f"no available capital in {req.track.value} bucket",
            )
        return None

    # ------------------------------------------------------------------
    # Check 6: Concentration (single-stock + sector + correlation)
    # ------------------------------------------------------------------

    def _check_concentration(self, req: TradeRequest) -> TradePermission | None:
        config = self._config
        total = self._live_total_capital

        # Single-stock: existing position + pending resting orders + this trade.
        # Use regime-scaled shares — concentration should reflect what will actually be ordered.
        risk_capital = self._ledger.bucket_capital(req.track) * config.risk_per_trade_pct(req.track)
        stop_dist = req.entry_price - req.stop_loss_price
        if stop_dist <= 0:
            return TradePermission.reject("trade_quality", "stop_loss_price >= entry_price")
        base_shares = int(risk_capital / stop_dist)
        regime_scale = REGIME_EXPOSURE_SCALE[req.current_regime]
        scaled_shares = int(base_shares * regime_scale)
        proposed_value = Decimal(str(max(scaled_shares, 1))) * req.entry_price
        combined_stock = req.existing_position_value + req.pending_order_value + proposed_value
        if combined_stock > total * config.single_stock_cap_pct:
            return TradePermission.reject(
                "concentration_single_stock",
                f"would breach {config.single_stock_cap_pct * 100:.0f}% single-stock cap",
            )

        # Sector cap
        sector_total = self._concentration.sector_deployed(req.sector) + proposed_value
        if sector_total > total * config.sector_cap_pct:
            return TradePermission.reject(
                "concentration_sector",
                f"would breach {config.sector_cap_pct * 100:.0f}% sector cap",
            )

        # Correlation cluster cap
        cluster_total = (
            self._concentration.correlation_cluster_deployed(req.stock_symbol, req.exchange)
            + proposed_value
        )
        if cluster_total > total * config.correlation_cluster_cap_pct:
            return TradePermission.reject(
                "concentration_correlation",
                f"would breach {config.correlation_cluster_cap_pct * 100:.0f}%"
                " correlation cluster cap",
            )

        return None

    # ------------------------------------------------------------------
    # Check 7: Trade quality (RR, EV)
    # ------------------------------------------------------------------

    def _check_trade_quality(self, req: TradeRequest) -> TradePermission | None:
        stop_dist = req.entry_price - req.stop_loss_price
        if stop_dist <= 0:
            return TradePermission.reject("trade_quality", "stop_loss_price >= entry_price")

        reward = req.target_price - req.entry_price
        rr = reward / stop_dist
        min_rr = MIN_RR[req.track]
        if rr < min_rr:
            return TradePermission.reject(
                "trade_quality_rr",
                f"RR={rr:.2f} below minimum {min_rr} for {req.track.value}",
            )

        # EV gate with per-track confidence haircut
        haircut = self._config.live_backtest_ratio(req.track)
        p_win = req.signal_confidence * haircut
        p_loss = Decimal("1") - p_win
        ev = (p_win * reward) - (p_loss * stop_dist)
        if ev <= 0:
            return TradePermission.reject(
                "trade_quality_ev",
                f"EV={ev:.2f} non-positive after {haircut * 100:.0f}% confidence haircut",
            )

        return None

    # ------------------------------------------------------------------
    # Check 8: Time checks
    # ------------------------------------------------------------------

    def _check_time(self, req: TradeRequest) -> TradePermission | None:
        if req.track == Track.INTRADAY and self._breakers.intraday_late_entry.value == "tripped":
            return TradePermission.reject("time_check", "intraday cutoff at 14:30 IST passed")
        return None

    # ------------------------------------------------------------------
    # Approval: compute position size
    # ------------------------------------------------------------------

    def _compute_approval(self, req: TradeRequest) -> TradePermission:
        bucket_capital = self._ledger.bucket_capital(req.track)
        risk_per_trade = bucket_capital * self._config.risk_per_trade_pct(req.track)
        stop_dist = req.entry_price - req.stop_loss_price
        shares = int(risk_per_trade / stop_dist)

        # Regime scale: reduce position size for non-bull-calm regimes
        scale = REGIME_EXPOSURE_SCALE[req.current_regime]
        shares = int(shares * scale)

        if shares < 1:
            return TradePermission.reject(
                "position_too_small",
                "computed position size < 1 share after regime scaling",
            )

        return TradePermission.approve(
            position_size_shares=shares,
            risk_per_trade_rupees=risk_per_trade,
        )
