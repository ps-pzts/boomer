"""Tests for PreTradeChecker — the 8-step pre-trade gate."""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from capital.circuit_breakers import CircuitBreakerState
from capital.models import (
    CapitalLedgerRow,
    Regime,
    RiskConfig,
    Track,
    TradeRequest,
)
from capital.pre_trade import PreTradeChecker
from capital.risk_config import RiskConfigStore
from capital.state import CapitalStateManager
from db.migrations import run_migrations

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"


class _NoConcentration:
    """Stub: nothing else deployed — all concentration checks pass trivially."""

    def sector_deployed(self, sector: str) -> Decimal:
        return Decimal("0")

    def correlation_cluster_deployed(self, symbol: str, exchange: str) -> Decimal:
        return Decimal("0")


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    run_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture()
def config(db: Path) -> RiskConfig:
    return RiskConfigStore(db).seed_defaults(date(2026, 1, 1))


@pytest.fixture()
def ledger(db: Path) -> CapitalLedgerRow:
    mgr = CapitalStateManager(db)
    return mgr.initialise(Decimal("50000"), date(2026, 1, 1))


def _make_checker(
    config: RiskConfig,
    ledger: CapitalLedgerRow,
    *,
    bot_mode: str = "auto",
    breakers: CircuitBreakerState | None = None,
    live_total_capital: Decimal | None = None,
    live_drawdown_pct: Decimal = Decimal("0"),
    live_intraday_pnl: Decimal = Decimal("0"),
) -> PreTradeChecker:
    if breakers is None:
        breakers = CircuitBreakerState.all_clear()
    return PreTradeChecker(
        config=config,
        breakers=breakers,
        ledger=ledger,
        concentration=_NoConcentration(),
        live_total_capital=live_total_capital or ledger.total_capital,
        live_drawdown_pct=live_drawdown_pct,
        live_intraday_pnl=live_intraday_pnl,
        bot_mode=bot_mode,
    )


def _make_request(
    track: Track = Track.SWING,
    entry: Decimal = Decimal("500"),
    stop: Decimal = Decimal("480"),
    target: Decimal = Decimal("560"),
    confidence: Decimal = Decimal("0.65"),
    regime: Regime = Regime.BULL_CALM,
) -> TradeRequest:
    return TradeRequest(
        stock_symbol="RELIANCE",
        exchange="NSE",
        track=track,
        entry_price=entry,
        stop_loss_price=stop,
        target_price=target,
        signal_confidence=confidence,
        sector="Energy",
        current_regime=regime,
        requested_at=datetime(2026, 1, 2, 4, 0, tzinfo=UTC),
    )


def test_valid_swing_trade_approved(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    checker = _make_checker(config, ledger)
    req = _make_request()
    perm = checker.check(req)
    assert perm.approved
    assert perm.position_size_shares >= 1
    assert perm.failed_check is None


def test_emergency_stop_rejects(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    checker = _make_checker(config, ledger, bot_mode="emergency_stop")
    perm = checker.check(_make_request())
    assert not perm.approved
    assert perm.failed_check == "bot_state"


def test_max_drawdown_rejects(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    from capital.circuit_breakers import BreakerStatus
    breakers = CircuitBreakerState(
        **{**CircuitBreakerState.all_clear().__dict__,
           "portfolio_max_drawdown": BreakerStatus.TRIPPED}
    )
    checker = _make_checker(config, ledger, breakers=breakers)
    perm = checker.check(_make_request())
    assert not perm.approved
    assert perm.failed_check == "bot_state"


def test_intraday_circuit_breaker_blocks_intraday_only(
    config: RiskConfig, ledger: CapitalLedgerRow
) -> None:
    from capital.circuit_breakers import BreakerStatus
    breakers = CircuitBreakerState(
        **{**CircuitBreakerState.all_clear().__dict__,
           "intraday_daily_loss": BreakerStatus.TRIPPED}
    )
    checker = _make_checker(config, ledger, breakers=breakers)
    assert not checker.check(_make_request(track=Track.INTRADAY)).approved
    assert checker.check(_make_request(track=Track.SWING)).approved


def test_rr_below_minimum_rejected(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    checker = _make_checker(config, ledger)
    # RR = (530 - 500) / (500 - 480) = 1.5; swing minimum is 1.5 → passes
    # RR = (510 - 500) / (500 - 480) = 0.5 → fails
    req = _make_request(entry=Decimal("500"), stop=Decimal("480"), target=Decimal("510"))
    perm = checker.check(req)
    assert not perm.approved
    assert perm.failed_check == "trade_quality_rr"


def test_negative_ev_rejected(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    checker = _make_checker(config, ledger)
    # Very low confidence → p_win small → EV negative
    req = _make_request(confidence=Decimal("0.05"))
    perm = checker.check(req)
    assert not perm.approved
    assert perm.failed_check == "trade_quality_ev"


def test_stop_above_entry_rejected(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    checker = _make_checker(config, ledger)
    req = _make_request(stop=Decimal("510"))  # stop > entry
    perm = checker.check(req)
    assert not perm.approved


def test_position_size_uses_risk_pct(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    """Worked example: swing bucket = ₹7,500, risk 1%, stop dist = ₹20 → 3 shares."""
    checker = _make_checker(config, ledger)
    # swing bucket = 15% × 50000 = 7500; risk_pct = 1% → risk = 75; stop dist = 20 → 3 shares
    req = _make_request(
        track=Track.SWING,
        entry=Decimal("500"),
        stop=Decimal("480"),   # stop dist = 20
        target=Decimal("560"), # RR = 60/20 = 3.0 ✓
        confidence=Decimal("0.65"),
    )
    perm = checker.check(req)
    assert perm.approved
    assert perm.position_size_shares == 3   # floor(75/20) = 3


def test_regime_reduces_position_size(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    """Regime scale reduces position size.

    Swing bucket = 15% × 50000 = 7500. Risk = 1% × 7500 = 75. Stop dist = 20 → 3 shares.
    Bull_calm (100%) → 3 shares. Bull_volatile (70%) → floor(3 × 0.70) = 2 shares.
    """
    req = _make_request(
        entry=Decimal("500"),
        stop=Decimal("480"),   # stop dist = 20, typical 2×ATR for swing
        target=Decimal("560"), # RR = 60/20 = 3.0 ✓
    )
    checker = _make_checker(config, ledger)
    perm_calm = checker.check(dataclasses.replace(req, current_regime=Regime.BULL_CALM))
    perm_volatile = checker.check(dataclasses.replace(req, current_regime=Regime.BULL_VOLATILE))
    assert perm_calm.approved
    assert perm_volatile.approved
    assert perm_volatile.position_size_shares < perm_calm.position_size_shares


def test_single_stock_concentration_breach_rejected(
    config: RiskConfig, ledger: CapitalLedgerRow
) -> None:
    # Existing + pending already at 4.9% of total; proposed would push past 5%
    existing = Decimal("50000") * Decimal("0.049")  # ₹2,450
    req = TradeRequest(
        stock_symbol="RELIANCE",
        exchange="NSE",
        track=Track.SWING,
        entry_price=Decimal("500"),
        stop_loss_price=Decimal("480"),
        target_price=Decimal("560"),
        signal_confidence=Decimal("0.65"),
        sector="Energy",
        current_regime=Regime.BULL_CALM,
        requested_at=datetime(2026, 1, 2, 4, 0, tzinfo=UTC),
        existing_position_value=existing,
        pending_order_value=Decimal("0"),
    )
    checker = _make_checker(config, ledger)
    perm = checker.check(req)
    assert not perm.approved
    assert perm.failed_check == "concentration_single_stock"


def test_intraday_late_entry_blocked(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    """intraday_late_entry breaker fires at track_cooldown check (check 2)."""
    from capital.circuit_breakers import BreakerStatus
    breakers = CircuitBreakerState(
        **{**CircuitBreakerState.all_clear().__dict__,
           "intraday_late_entry": BreakerStatus.TRIPPED}
    )
    checker = _make_checker(config, ledger, breakers=breakers)
    perm = checker.check(_make_request(track=Track.INTRADAY))
    assert not perm.approved
    assert perm.failed_check == "track_cooldown"
