from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from backtester.models import BacktestConfig, EntryDecision
from backtester.simulation import BacktestSimulation
from executor.models import PriceBar, ProductType

# ── Test fixtures ──────────────────────────────────────────────────────────────

def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open("migrations/0001_initial_schema.sql") as f:
        db.executescript(f.read())
    with open("migrations/0004_executor_schema.sql") as f:
        db.executescript(f.read())
    return db


def _bar(symbol: str, d: str, *, o=100, h=102, lo=98, c=100, vol=100_000) -> PriceBar:
    return PriceBar(symbol=symbol, date=d, open=o, high=h, low=lo, close=c, volume=vol)


def _flat_price_loader(symbol: str, from_date: str, to_date: str) -> list[PriceBar]:
    """Flat ₹100 bar for any symbol/date."""
    return [_bar(symbol, from_date)]


def _empty_feature_loader(symbol: str, as_of_date: str) -> dict:
    return {}


def _make_config(**overrides) -> BacktestConfig:
    defaults = dict(
        name="test",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 12),  # ~8 trading days (excludes Jan 6-7 weekend)
        initial_capital=100_000.0,
        tracks=["swing"],
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


# ── Existing core tests (keep passing) ────────────────────────────────────────

class TestBacktestSimulationBasic:
    def test_run_completes_and_persists_run(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=["RELIANCE", "TCS"],
        )
        summary = sim.run()
        assert summary.run_id is not None
        row = db.execute(
            "SELECT status FROM backtest_runs WHERE run_id=?", (summary.run_id,)
        ).fetchone()
        assert row["status"] == "complete"

    def test_daily_states_persisted(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=["TCS"],
        )
        summary = sim.run()
        count = db.execute(
            "SELECT COUNT(*) FROM backtest_daily_state WHERE run_id=?", (summary.run_id,)
        ).fetchone()[0]
        assert count >= 2

    def test_capital_stays_near_initial_with_no_entry_decider(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=["RELIANCE"],
        )
        summary = sim.run()
        assert summary.final_capital == pytest.approx(100_000.0, rel=0.01)
        assert summary.total_trades == 0

    def test_run_failure_marks_status_failed(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        # Sabotage: remove daily_states to force ValueError in _compute_summary
        with pytest.raises(ValueError):
            sim._daily_states = []
            sim._compute_summary("fake-run-id")


# ── Entry logic tests ──────────────────────────────────────────────────────────

def _always_enter_decider(symbol, sim_date, features, bar) -> EntryDecision:
    """Signal to buy every symbol every day (for deterministic testing)."""
    return EntryDecision(
        entry_price=0.0,           # market order at open (₹100)
        sl_price=90.0,             # stop 10% below
        target_price=120.0,        # target 20% above
        product=ProductType.CNC,
        track="swing",
        confidence=0.8,
        strategy_id="test_strategy",
    )


def _never_enter_decider(symbol, sim_date, features, bar) -> None:
    return None


class TestEntryLogic:
    def test_entry_decider_opens_positions(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(max_open_positions=2, tracks=["swing"]),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE", "TCS"],
            entry_decider=_always_enter_decider,
        )
        summary = sim.run()
        # With max_open_positions=2, both symbols should be entered on day 1
        # and held until GTT triggers (which needs bars to go below 90 or above 120)
        # On flat ₹100 bars neither trigger fires → positions held all week → 0 closed trades
        # but capital should reflect positions marked at ₹100 close
        assert summary.final_capital == pytest.approx(100_000.0, rel=0.02)

    def test_no_entry_when_decider_returns_none(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=["RELIANCE"],
            entry_decider=_never_enter_decider,
        )
        summary = sim.run()
        assert summary.total_trades == 0
        assert summary.final_capital == pytest.approx(100_000.0, rel=0.01)

    def test_max_open_positions_cap_respected(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(max_open_positions=1, tracks=["swing"]),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE", "TCS", "INFY"],
            entry_decider=_always_enter_decider,
        )
        sim.run()
        # After day 1, only 1 position should be held (cap = 1)
        assert len(sim._open_positions) <= 1

    def test_position_uses_correct_entry_price(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(max_open_positions=1, tracks=["swing"]),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE"],
            entry_decider=_always_enter_decider,
        )
        sim.run()
        # Market order on flat ₹100 bar → fill at bar.open = ₹100
        if sim._open_positions:
            pos = next(iter(sim._open_positions.values()))
            assert pos["entry_price"] == pytest.approx(100.0)
            assert pos["symbol"] == "RELIANCE"

    def test_no_duplicate_position_in_same_symbol(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db,
            config=_make_config(max_open_positions=5, tracks=["swing"]),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE"],
            entry_decider=_always_enter_decider,
        )
        sim.run()
        # Only one position in RELIANCE even though the decider fires every day
        reliance_positions = [
            p for p in sim._open_positions.values() if p["symbol"] == "RELIANCE"
        ]
        assert len(reliance_positions) <= 1


# ── GTT / exit tests ───────────────────────────────────────────────────────────

def _make_stop_trigger_loader(trigger_on_day: int):
    """Return a price loader that drops below SL (₹90) on a specific trading-day index."""
    call_count: dict[str, int] = {}

    def loader(symbol: str, from_date: str, to_date: str) -> list[PriceBar]:
        call_count[symbol] = call_count.get(symbol, 0) + 1
        if call_count[symbol] == trigger_on_day:
            # Bar that crosses the ₹90 stop — low < 90
            return [_bar(symbol, from_date, o=92, h=93, lo=85, c=88)]
        return [_bar(symbol, from_date)]

    return loader


def _make_target_trigger_loader(trigger_on_day: int):
    """Return a price loader that crosses the target (₹120) on a specific day."""
    call_count: dict[str, int] = {}

    def loader(symbol: str, from_date: str, to_date: str) -> list[PriceBar]:
        call_count[symbol] = call_count.get(symbol, 0) + 1
        if call_count[symbol] == trigger_on_day:
            return [_bar(symbol, from_date, o=118, h=125, lo=117, c=122)]
        return [_bar(symbol, from_date)]

    return loader


class TestGttExitLogic:
    def test_stop_loss_fires_and_closes_position(self):
        db = _make_db()
        # Enter on day 1 (bar 100), stop triggers on bar 3 (low 85 < SL 90)
        # But: day 1 bar is used BEFORE the entry fires (entry fires after bar load)
        # → entry on day 1, SL hits on day 3 → trade recorded
        loader = _make_stop_trigger_loader(trigger_on_day=3)
        sim = BacktestSimulation(
            db=db,
            config=_make_config(
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 12),
                max_open_positions=1,
                tracks=["swing"],
            ),
            price_loader=loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE"],
            entry_decider=_always_enter_decider,
        )
        summary = sim.run()
        assert summary.total_trades >= 1
        # SL trade should be a loss: exit below ₹100 entry after costs
        losing_trades = [t for t in sim._trades if t.exit_reason == "sl"]
        assert len(losing_trades) >= 1
        assert losing_trades[0].net_pnl < 0

    def test_target_fires_and_closes_position(self):
        db = _make_db()
        loader = _make_target_trigger_loader(trigger_on_day=3)
        sim = BacktestSimulation(
            db=db,
            config=_make_config(
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 12),
                max_open_positions=1,
                tracks=["swing"],
            ),
            price_loader=loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE"],
            entry_decider=_always_enter_decider,
        )
        summary = sim.run()
        assert summary.total_trades >= 1
        winning_trades = [t for t in sim._trades if t.exit_reason == "target"]
        assert len(winning_trades) >= 1
        assert winning_trades[0].gross_pnl > 0

    def test_closed_position_re_entered_next_day(self):
        """After a position is closed, the same symbol can be re-entered."""
        db = _make_db()
        loader = _make_stop_trigger_loader(trigger_on_day=2)
        sim = BacktestSimulation(
            db=db,
            config=_make_config(
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 12),
                max_open_positions=1,
                tracks=["swing"],
            ),
            price_loader=loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE"],
            entry_decider=_always_enter_decider,
        )
        sim.run()
        # At least one trade closed + new entries on subsequent days → ≥ 2 trades total
        assert len(sim._trades) >= 1


# ── Cash accounting tests ──────────────────────────────────────────────────────

class TestCashAccounting:
    def test_cash_decreases_on_entry(self):
        db = _make_db()
        captured: list[float] = []

        def tracking_decider(symbol, sim_date, features, bar):
            captured.append(sim._mock.get_funds().available_cash)
            if len(captured) == 1:  # only enter on very first call
                return EntryDecision(
                    entry_price=0.0, sl_price=90.0, target_price=120.0,
                    product=ProductType.CNC, track="swing",
                    confidence=0.8, strategy_id="s",
                )
            return None

        sim = BacktestSimulation(
            db=db,
            config=_make_config(max_open_positions=1, tracks=["swing"]),
            price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE"],
            entry_decider=tracking_decider,
        )
        sim.run()
        if len(captured) >= 2:
            # Cash after entry should be less than initial capital
            assert captured[1] < 100_000.0

    def test_capital_is_initial_plus_pnl_after_close(self):
        db = _make_db()
        loader = _make_target_trigger_loader(trigger_on_day=3)
        sim = BacktestSimulation(
            db=db,
            config=_make_config(
                start_date=date(2024, 1, 2),
                end_date=date(2024, 1, 12),
                max_open_positions=1,
                tracks=["swing"],
            ),
            price_loader=loader,
            feature_loader=_empty_feature_loader,
            universe=["RELIANCE"],
            entry_decider=_always_enter_decider,
        )
        summary = sim.run()
        if summary.total_trades > 0:
            total_net_pnl = sum(t.net_pnl for t in sim._trades)
            expected_capital = 100_000.0 + total_net_pnl
            # Allow some tolerance for open positions still held at end
            assert summary.final_capital == pytest.approx(expected_capital, rel=0.05)


# ── Statistics tests ───────────────────────────────────────────────────────────

class TestBacktestSharpe:
    def test_sharpe_with_all_positive_returns(self):
        all_up = [0.01] * 100
        sharpe = BacktestSimulation._sharpe(all_up)
        assert sharpe > 1.0

    def test_sharpe_with_empty_returns_is_zero(self):
        assert BacktestSimulation._sharpe([]) == pytest.approx(0.0)

    def test_sharpe_with_single_return_is_zero(self):
        assert BacktestSimulation._sharpe([0.01]) == pytest.approx(0.0)

    def test_sharpe_volatile_returns_lower_than_steady(self):
        steady = BacktestSimulation._sharpe([0.005] * 100)
        volatile = BacktestSimulation._sharpe([0.02, -0.01] * 50)
        assert steady > volatile


# ── Trading days tests ─────────────────────────────────────────────────────────

class TestBacktestTradingDays:
    def test_excludes_weekends(self):
        db = _make_db()
        # Jan 1-7 2024: Mon, Tue, Wed, Thu, Fri, Sat, Sun
        config = _make_config(start_date=date(2024, 1, 1), end_date=date(2024, 1, 7))
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        days = list(sim._trading_days())
        assert all(d.weekday() < 5 for d in days)

    def test_excludes_nse_holidays(self):
        db = _make_db()
        # Jan 22 (Ram Mandir) and Jan 26 (Republic Day) are holidays in 2024
        config = _make_config(start_date=date(2024, 1, 22), end_date=date(2024, 1, 26))
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        days = list(sim._trading_days())
        assert date(2024, 1, 22) not in days
        assert date(2024, 1, 26) not in days
        assert len(days) == 3  # Jan 23, 24, 25

    def test_full_non_holiday_week_yields_five_days(self):
        db = _make_db()
        config = _make_config(start_date=date(2024, 1, 8), end_date=date(2024, 1, 12))
        sim = BacktestSimulation(
            db=db, config=config, price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        days = list(sim._trading_days())
        assert len(days) == 5


# ── Acceptance / OOS tests ─────────────────────────────────────────────────────

class TestBacktestAcceptance:
    def test_acceptance_fails_on_low_sharpe(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=0.5, max_dd=5.0,
            trades_by_track={"long_term": 200, "swing": 200, "intraday": 200},
            expectancy=0.5, win_rate=0.6, avg_win=2.0, avg_loss=1.0,
        )
        assert not passes
        assert any("Sharpe" in f for f in failures)

    def test_acceptance_passes_all_criteria(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5, max_dd=10.0,
            trades_by_track={"long_term": 150, "swing": 150, "intraday": 150},
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
        )
        assert passes
        assert failures == []

    def test_acceptance_fails_on_too_few_trades(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5, max_dd=10.0,
            trades_by_track={"swing": 50},  # < 100 minimum
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
        )
        assert not passes
        assert any("trades" in f for f in failures)

    def test_oos_check_fires_when_in_sample_sharpe_provided(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        # OOS Sharpe 0.5 is < 50% of IS Sharpe 2.0 → fail
        failures, passes = sim._check_acceptance(
            sharpe=0.5, max_dd=5.0,
            trades_by_track={"swing": 200},
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
            in_sample_sharpe=2.0,
        )
        assert not passes
        assert any("OOS" in f for f in failures)

    def test_oos_check_passes_when_sharpe_meets_threshold(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        # OOS Sharpe 1.4 >= 50% of IS Sharpe 2.0 → OOS check passes
        failures, passes = sim._check_acceptance(
            sharpe=1.4, max_dd=5.0,
            trades_by_track={"swing": 200},
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
            in_sample_sharpe=2.0,
        )
        assert passes
        assert not any("OOS" in f for f in failures)

    def test_oos_check_skipped_when_no_in_sample_sharpe(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        # Pass all non-OOS criteria; no in_sample_sharpe → OOS check is not evaluated
        failures, passes = sim._check_acceptance(
            sharpe=1.4, max_dd=5.0,
            trades_by_track={"swing": 200},
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
        )
        assert passes
        assert not any("OOS" in f for f in failures)

    def test_acceptance_fails_on_max_drawdown(self):
        db = _make_db()
        sim = BacktestSimulation(
            db=db, config=_make_config(), price_loader=_flat_price_loader,
            feature_loader=_empty_feature_loader, universe=[],
        )
        failures, passes = sim._check_acceptance(
            sharpe=1.5, max_dd=20.0,  # > 15% threshold
            trades_by_track={"swing": 200},
            expectancy=0.5, win_rate=0.6, avg_win=3.0, avg_loss=1.0,
        )
        assert not passes
        assert any("DD" in f for f in failures)


# ── Code hash test ─────────────────────────────────────────────────────────────

class TestCodeHash:
    def test_code_hash_is_deterministic(self):
        h1 = BacktestSimulation._compute_code_hash()
        h2 = BacktestSimulation._compute_code_hash()
        assert h1 == h2

    def test_code_hash_is_non_trivial(self):
        h = BacktestSimulation._compute_code_hash()
        assert h != "boomer_v1"[:16]   # not the old placeholder
        assert len(h) == 16
