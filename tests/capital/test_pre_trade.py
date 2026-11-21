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


# ── boundaries and messages (#89) ────────────────────────────────────────────
# Base case (capital 50,000, risk 0.5% = 250, entry 500, stop 425 -> 75/share):
#   3 shares x 500 = 1,500 proposed. Caps: stock 5% = 2,500; sector 25% = 12,500;
#   cluster 35% = 17,500. Regime scale: calm 1.0, volatile 0.7, bear 0.3.


class _Concentration:
    def __init__(self, sector: str = "0", cluster: str = "0"):
        self._s, self._c = Decimal(sector), Decimal(cluster)

    def sector_deployed(self, sector: str) -> Decimal:
        return self._s

    def correlation_cluster_deployed(self, symbol: str, exchange: str) -> Decimal:
        return self._c


def _checker(config, ledger, concentration=None, breakers=None) -> PreTradeChecker:
    c = _make_checker(config, ledger, breakers=breakers)
    if concentration is not None:
        c._concentration = concentration
    return c


def _req_with(**kw) -> TradeRequest:
    return dataclasses.replace(_make_request(), **kw)


def _tripped(name: str) -> CircuitBreakerState:
    from capital.circuit_breakers import BreakerStatus

    return CircuitBreakerState(
        **{**CircuitBreakerState.all_clear().__dict__, name: BreakerStatus.TRIPPED}
    )


class TestRegimeGate:
    # BULL_VOLATILE allows 70% of the 50,000 bucket = 35,000 deployed
    def test_at_the_allowed_deployment_is_refused(self, config, ledger):
        req = _make_request(regime=Regime.BULL_VOLATILE)
        full = dataclasses.replace(ledger, intraday_deployed=Decimal("35000"))
        perm = _checker(config, full).check(req)
        assert perm.failed_check == "regime_gate"
        assert perm.reason == (
            "regime=bull_volatile caps intraday deployment at 70% of bucket; already at limit"
        )
        just_under = dataclasses.replace(ledger, intraday_deployed=Decimal("34999"))
        ok = _checker(config, just_under).check(req)
        assert ok.approved and ok.position_size_shares == 2  # int(3 x 0.7)

    def test_a_calm_market_allows_the_whole_bucket_but_not_more(self, config, ledger):
        req = _make_request(regime=Regime.BULL_CALM)
        full = dataclasses.replace(ledger, intraday_deployed=Decimal("50000"))
        assert _checker(config, full).check(req).failed_check == "regime_gate"

    def test_bear_market_caps_at_30pct(self, config, ledger):
        req = _make_request(regime=Regime.BEAR)
        at_cap = dataclasses.replace(ledger, intraday_deployed=Decimal("15000"))  # 30% of 50,000
        assert _checker(config, at_cap).check(req).failed_check == "regime_gate"


class TestConcentration:
    def test_stop_equal_to_entry_is_a_quality_reject_not_a_crash(self, config, ledger):
        perm = _checker(config, ledger).check(_make_request(stop=Decimal("500")))
        assert (perm.failed_check, perm.reason) == (
            "trade_quality",
            "stop_loss_price >= entry_price",
        )

    def test_one_rupee_stop_is_valid(self, config, ledger):
        # entry 10, stop 9: risk 250 / 1 = 250 shares x 10 = 2,500 = exactly the 5% cap
        req = _make_request(entry=Decimal("10"), stop=Decimal("9"), target=Decimal("12"))
        perm = _checker(config, ledger).check(req)
        assert perm.approved and perm.position_size_shares == 250

    def test_single_stock_cap_counts_existing_and_pending_and_is_inclusive(self, config, ledger):
        c = _checker(config, ledger)
        # 1,500 proposed + existing 1,000 = 2,500: at the cap, allowed
        assert c.check(_req_with(existing_position_value=Decimal("1000"))).approved
        assert c.check(_req_with(pending_order_value=Decimal("1000"))).approved
        both = c.check(
            _req_with(existing_position_value=Decimal("500"), pending_order_value=Decimal("501"))
        )
        assert (both.failed_check, both.reason) == (
            "concentration_single_stock",
            "would breach 5% single-stock cap",
        )
        assert c.check(_req_with(pending_order_value=Decimal("1001"))).failed_check == (
            "concentration_single_stock"
        )

    def test_regime_scaled_size_is_what_counts(self, config, ledger):
        # BEAR: int(3 x 0.3) = 0 -> counted as 1 share (500). Existing 2,000 -> 2,500: fits
        # (the unscaled 3 shares = 1,500 would breach). Then the order itself is too small.
        perm = _checker(config, ledger).check(
            _req_with(current_regime=Regime.BEAR, existing_position_value=Decimal("2000"))
        )
        assert perm.failed_check == "position_too_small"
        # 2,001 + 500 > 2,500
        perm = _checker(config, ledger).check(
            _req_with(current_regime=Regime.BEAR, existing_position_value=Decimal("2001"))
        )
        assert perm.failed_check == "concentration_single_stock"

    def test_sector_cap_boundary_and_message(self, config, ledger):
        ok = _checker(config, ledger, _Concentration(sector="11000")).check(_make_request())
        assert ok.approved  # 11,000 + 1,500 = 12,500 = 25% of 50,000
        perm = _checker(config, ledger, _Concentration(sector="11001")).check(_make_request())
        assert (perm.failed_check, perm.reason) == (
            "concentration_sector",
            "would breach 25% sector cap",
        )
        # a sector already deployed counts upward (11,000 - 1,500 would hide it)
        perm = _checker(config, ledger, _Concentration(sector="12000")).check(_make_request())
        assert perm.failed_check == "concentration_sector"

    def test_correlation_cluster_cap_boundary_and_message(self, config, ledger):
        ok = _checker(config, ledger, _Concentration(cluster="16000")).check(_make_request())
        assert ok.approved  # 16,000 + 1,500 = 17,500 = 35%
        perm = _checker(config, ledger, _Concentration(cluster="16001")).check(_make_request())
        assert (perm.failed_check, perm.reason) == (
            "concentration_correlation",
            "would breach 35% correlation cluster cap",
        )
        perm = _checker(config, ledger, _Concentration(cluster="17000")).check(_make_request())
        assert perm.failed_check == "concentration_correlation"


class TestTradeQuality:
    def test_rr_exactly_at_minimum_passes(self, config, ledger):
        # reward 112.5 / risk 75 = 1.5
        c = _checker(config, ledger)
        assert c.check(_make_request(target=Decimal("612.5"))).approved
        below = c.check(_make_request(target=Decimal("612.4")))
        assert below.failed_check == "trade_quality_rr"
        assert "below minimum 1.5" in below.reason

    def test_ev_uses_the_haircut_confidence(self, config, ledger):
        # confidence 0.5 x haircut 0.7 = 0.35; reward 112.5, risk 75:
        # EV = 0.35 x 112.5 - 0.65 x 75 = 39.375 - 48.75 = -9.375 -> rejected.
        # (dividing by the haircut instead would read 0.714 and approve a losing trade)
        perm = _checker(config, ledger).check(
            _make_request(target=Decimal("612.5"), confidence=Decimal("0.5"))
        )
        assert perm.failed_check == "trade_quality_ev"
        assert perm.reason.startswith("EV=-9.3")
        assert "after 70% confidence haircut" in perm.reason

    def test_zero_ev_is_rejected(self, config, ledger):
        # haircut 0.8, confidence 0.5 -> p 0.4; EV = 0.4 x 112.5 - 0.6 x 75 = 45 - 45 = 0
        cfg = dataclasses.replace(config, live_backtest_ratio_intraday=Decimal("0.8"))
        perm = _checker(cfg, ledger).check(
            _make_request(target=Decimal("612.5"), confidence=Decimal("0.5"))
        )
        assert perm.failed_check == "trade_quality_ev"
        assert perm.reason.startswith("EV=0.00")

    def test_stop_at_entry_rejected_by_the_quality_check_too(self, config, ledger):
        c = _checker(config, ledger)
        perm = c._check_trade_quality(_make_request(stop=Decimal("500")))
        assert (perm.failed_check, perm.reason) == (
            "trade_quality",
            "stop_loss_price >= entry_price",
        )
        # and a 1-rupee stop is a valid stop for this check
        ok = _make_request(entry=Decimal("10"), stop=Decimal("9"), target=Decimal("12"))
        assert c._check_trade_quality(ok) is None


class TestUnreachableByCheckOrder:
    """Rules that check() can never reach because an earlier rule catches the same
    situation first; tested directly so each rule itself is still pinned (#89)."""

    def test_black_swan_is_caught_earlier_by_track_cooldown(self, config, ledger):
        c = _checker(config, ledger, breakers=_tripped("black_swan"))
        assert c.check(_make_request()).failed_check == "track_cooldown"
        perm = c._check_black_swan(_make_request())
        assert (perm.failed_check, perm.reason) == (
            "black_swan",
            "Nifty black-swan move — manual resume",
        )
        assert _checker(config, ledger)._check_black_swan(_make_request()) is None

    def test_full_bucket_is_caught_earlier_by_the_regime_gate(self, config, ledger):
        full = dataclasses.replace(ledger, intraday_deployed=Decimal("50000"))
        c = _checker(config, full)
        assert c.check(_make_request()).failed_check == "regime_gate"
        perm = c._check_capital_availability(_make_request())
        assert (perm.failed_check, perm.reason) == (
            "capital_availability",
            "no available capital in intraday bucket",
        )
        over = dataclasses.replace(ledger, intraday_deployed=Decimal("50001"))
        assert _checker(config, over)._check_capital_availability(_make_request()) is not None

    def test_a_fraction_of_a_rupee_available_is_still_available(self, config, ledger):
        tiny = dataclasses.replace(ledger, intraday_deployed=Decimal("49999.50"))
        assert _checker(config, tiny)._check_capital_availability(_make_request()) is None

    def test_late_entry_time_check(self, config, ledger):
        c = _checker(config, ledger, breakers=_tripped("intraday_late_entry"))
        perm = c._check_time(_make_request())
        assert (perm.failed_check, perm.reason) == (
            "time_check",
            "intraday cutoff at 14:30 IST passed",
        )
        assert _checker(config, ledger)._check_time(_make_request()) is None


class TestApproval:
    def test_size_and_risk_rupees(self, config, ledger):
        perm = _checker(config, ledger).check(_make_request())
        assert (perm.position_size_shares, perm.risk_per_trade_rupees) == (3, Decimal("250.000"))

    def test_exactly_one_share_is_approved(self, config, ledger):
        # entry 1,000, stop 750 -> 250/share: 250 / 250 = 1 share
        req = _make_request(entry=Decimal("1000"), stop=Decimal("750"), target=Decimal("1400"))
        perm = _checker(config, ledger).check(req)
        assert perm.approved and perm.position_size_shares == 1

    def test_less_than_one_share_after_scaling_is_refused(self, config, ledger):
        # BEAR: int(3 x 0.3) = 0 shares
        perm = _checker(config, ledger).check(_make_request(regime=Regime.BEAR))
        assert (perm.failed_check, perm.approved) == ("position_too_small", False)
        assert perm.reason == "computed position size < 1 share after regime scaling"
