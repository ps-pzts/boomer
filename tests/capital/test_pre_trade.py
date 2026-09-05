"""Tests for PreTradeChecker — the 8-step pre-trade gate."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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

IST = ZoneInfo("Asia/Kolkata")

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


# Default worked example: intraday bucket = 100% x 50000 = 50000.
# risk = 0.5% x 50000 = 250. stop_dist = 75 -> shares = floor(250/75) = 3.
# proposed_value = 3 x 500 = 1500 = 3% of total capital (safely under the 5% single-stock cap,
# so tests that don't care about concentration don't trip it incidentally — the concentration
# check runs BEFORE the RR/EV trade-quality check, so any test overriding stop/target needs to
# keep shares x entry_price under the cap unless it's deliberately testing the cap itself).
def _make_request(
    track: Track = Track.INTRADAY,
    entry: Decimal = Decimal("500"),
    stop: Decimal = Decimal("425"),
    target: Decimal = Decimal("650"),
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
        requested_at=datetime(2026, 1, 2, 4, 0, tzinfo=IST),
    )


def test_valid_intraday_trade_approved(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
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
        **{
            **CircuitBreakerState.all_clear().__dict__,
            "portfolio_max_drawdown": BreakerStatus.TRIPPED,
        }
    )
    checker = _make_checker(config, ledger, breakers=breakers)
    perm = checker.check(_make_request())
    assert not perm.approved
    assert perm.failed_check == "bot_state"


def test_intraday_circuit_breaker_blocks_intraday(
    config: RiskConfig, ledger: CapitalLedgerRow
) -> None:
    from capital.circuit_breakers import BreakerStatus

    breakers = CircuitBreakerState(
        **{**CircuitBreakerState.all_clear().__dict__, "intraday_daily_loss": BreakerStatus.TRIPPED}
    )
    checker = _make_checker(config, ledger, breakers=breakers)
    assert not checker.check(_make_request(track=Track.INTRADAY)).approved


def test_rr_below_minimum_rejected(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    checker = _make_checker(config, ledger)
    # stop_dist=60 -> shares=floor(250/60)=4, value=2000 (4% of 50000) stays under the 5%
    # single-stock cap so this test hits the RR check, not concentration, as intended.
    # RR = (520 - 500) / (500 - 440) = 20/60 = 0.33 < 1.5 minimum -> fails
    req = _make_request(entry=Decimal("500"), stop=Decimal("440"), target=Decimal("520"))
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
    """Worked example: intraday bucket = ₹50,000, risk 0.5%, stop dist = ₹75 → 3 shares."""
    checker = _make_checker(config, ledger)
    # intraday bucket = 100% × 50000 = 50000; risk_pct = 0.5% → risk = 250; stop 75 → 3 shares
    req = _make_request(
        entry=Decimal("500"),
        stop=Decimal("425"),  # stop dist = 75
        target=Decimal("650"),  # RR = 150/75 = 2.0 ✓
        confidence=Decimal("0.65"),
    )
    perm = checker.check(req)
    assert perm.approved
    assert perm.position_size_shares == 3  # floor(250/75) = 3


def test_regime_reduces_position_size(config: RiskConfig, ledger: CapitalLedgerRow) -> None:
    """Regime scale reduces position size.

    Intraday bucket = 100% × 50000 = 50000. Risk = 0.5% × 50000 = 250. Stop dist = 75 → 3 shares.
    Bull_calm (100%) → 3 shares. Bull_volatile (70%) → floor(3 × 0.70) = 2 shares.
    """
    req = _make_request(
        entry=Decimal("500"),
        stop=Decimal("425"),  # stop dist = 75
        target=Decimal("650"),  # RR = 150/75 = 2.0 ✓
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
    # Default request proposes 3 shares × ₹500 = ₹1,500 (3% of 50000).
    # Existing position pushes combined exposure past the 5% (₹2,500) cap.
    existing = Decimal("1100")
    req = TradeRequest(
        stock_symbol="RELIANCE",
        exchange="NSE",
        track=Track.INTRADAY,
        entry_price=Decimal("500"),
        stop_loss_price=Decimal("425"),
        target_price=Decimal("650"),
        signal_confidence=Decimal("0.65"),
        sector="Energy",
        current_regime=Regime.BULL_CALM,
        requested_at=datetime(2026, 1, 2, 4, 0, tzinfo=IST),
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
        **{**CircuitBreakerState.all_clear().__dict__, "intraday_late_entry": BreakerStatus.TRIPPED}
    )
    checker = _make_checker(config, ledger, breakers=breakers)
    perm = checker.check(_make_request(track=Track.INTRADAY))
    assert not perm.approved
    assert perm.failed_check == "track_cooldown"
