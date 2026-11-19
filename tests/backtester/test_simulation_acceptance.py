"""Acceptance-gate tests for BacktestSimulation._check_acceptance (incl. OOS gate)."""

from __future__ import annotations

from backtester.simulation import BacktestSimulation
from tests.backtester.sim_helpers import (
    _empty_feature_loader,
    _flat_price_loader,
    _make_config,
    _make_db,
)


class TestBacktestAcceptance:
    def test_acceptance_fails_on_low_sharpe(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=0.5,
            max_dd=5.0,
            trades_by_track={"intraday": 600},
            expectancy=0.5,
            win_rate=0.6,
            avg_win=2.0,
            avg_loss=1.0,
        )
        assert not passes
        assert any("Sharpe" in f for f in failures)

    def test_acceptance_passes_all_criteria(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5,
            max_dd=10.0,
            trades_by_track={"intraday": 450},
            expectancy=0.5,
            win_rate=0.6,
            avg_win=3.0,
            avg_loss=1.0,
        )
        assert passes
        assert failures == []

    def test_acceptance_fails_on_too_few_trades(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5,
            max_dd=10.0,
            trades_by_track={"intraday": 50},  # < 100 minimum
            expectancy=0.5,
            win_rate=0.6,
            avg_win=3.0,
            avg_loss=1.0,
        )
        assert not passes
        assert any("trades" in f for f in failures)

    def test_oos_check_fires_when_in_sample_sharpe_provided(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=[],
        )
        # OOS Sharpe 0.5 is < 50% of IS Sharpe 2.0 → fail
        failures, passes = sim._check_acceptance(
            sharpe=0.5,
            max_dd=5.0,
            trades_by_track={"intraday": 200},
            expectancy=0.5,
            win_rate=0.6,
            avg_win=3.0,
            avg_loss=1.0,
            in_sample_sharpe=2.0,
        )
        assert not passes
        assert any("OOS" in f for f in failures)

    def test_oos_check_passes_when_sharpe_meets_threshold(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=[],
        )
        # OOS Sharpe 1.4 >= 50% of IS Sharpe 2.0 → OOS check passes
        failures, passes = sim._check_acceptance(
            sharpe=1.4,
            max_dd=5.0,
            trades_by_track={"intraday": 200},
            expectancy=0.5,
            win_rate=0.6,
            avg_win=3.0,
            avg_loss=1.0,
            in_sample_sharpe=2.0,
        )
        assert passes
        assert not any("OOS" in f for f in failures)

    def test_oos_check_skipped_when_no_in_sample_sharpe(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=[],
        )
        # Pass all non-OOS criteria; no in_sample_sharpe → OOS check is not evaluated
        failures, passes = sim._check_acceptance(
            sharpe=1.4,
            max_dd=5.0,
            trades_by_track={"intraday": 200},
            expectancy=0.5,
            win_rate=0.6,
            avg_win=3.0,
            avg_loss=1.0,
        )
        assert passes
        assert not any("OOS" in f for f in failures)

    def test_acceptance_fails_on_max_drawdown(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5,
            max_dd=20.0,  # > 15% threshold
            trades_by_track={"intraday": 200},
            expectancy=0.5,
            win_rate=0.6,
            avg_win=3.0,
            avg_loss=1.0,
        )
        assert not passes
        assert any("DD" in f for f in failures)


# ── Code hash test ─────────────────────────────────────────────────────────────
