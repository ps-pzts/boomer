"""brain/intraday_decision.decide() — enter or not, and how (#69).

Worked example (₹10 L capital, defaults, bull_calm), LTP 1,000, ATR 20, ORB high 1,005:
  ID1 trigger = 1,005 × 1.001 = 1,006.00;  limit = ceil_tick(1,006 × 1.002) = 1,008.05
  plan at the limit: stop = 1,008.05 − 1.5 × 20 = 978.05;  target = 1,008.05 + 1.5 × 30 = 1,053.05
  size: risk 5,000 / 30 = 166 shares; cap 50,000 / 1,008.05 = 49 -> 49 (cap binds), risk ₹1,470
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from brain.intraday_decision import EntryDecision, Rejection, decide
from brain.models import EntryStrategy
from capital.risk_config import RiskConfigStore
from db.migrations import run_migrations
from tests.executor.test_session_inputs import _MIGRATIONS, DAY, _ledger

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 19, 9, 45, tzinfo=IST)

FEATURES = dict(
    atr_14d=20.0,
    premarket_gap_pct=0.5,
    overnight_news_sentiment=0.5,
    orb_range_vs_atr=0.5,
    orb_high=1005.0,
    orb_low=995.0,
    fo_oi_overnight_change_pct=5.0,
    minutes_since_latest_news=30.0,
    nifty_intraday_direction=1.0,
    beta_20d=1.0,
    bid_ask_spread_pct=0.05,
    vwap_current=998.0,
    price_vs_open_pct=0.2,
    minutes_since_market_open=30.0,
    avg_daily_volume_20d=1e6,
    avg_traded_value_20d=5e9,
)


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("cfg") / "c.db")
    run_migrations(db, _MIGRATIONS)
    return RiskConfigStore(db).seed_defaults(DAY)


def _decide(config, ltp=1000.0, capital="1000000", regime="bull_calm", **overrides):
    return decide(
        symbol="INFY",
        exchange="NSE",
        features={**FEATURES, **overrides},
        ltp=ltp,
        regime=regime,
        now=NOW,
        config=config,
        ledger=_ledger(capital=capital),
    )


class TestOrb:
    def test_worked_example_is_a_stop_limit_breakout(self, config):
        d = _decide(config)
        assert isinstance(d, EntryDecision)
        assert d.strategy == EntryStrategy.ID1
        assert (d.entry_trigger, d.entry_price) == (Decimal("1006.00"), Decimal("1008.05"))
        assert (d.stop_price, d.target_price) == (Decimal("978.05"), Decimal("1053.05"))
        assert (d.quantity, d.sizing.binding) == (49, "single_stock_cap")
        assert d.sizing.risk_rupees == Decimal("1470.00")

    def test_plan_is_anchored_at_the_entry_not_the_ltp(self, config):
        """Stop/target from the price we'd pay (1,008.05), not the LTP (1,000)."""
        d = _decide(config)
        assert d.entry_price - d.stop_price == Decimal("30.00")  # 1.5 × ATR from the entry
        assert d.plan.reward_to_risk == Decimal("1.5")

    def test_already_broken_out_within_band_is_a_limit(self, config):
        d = _decide(config, ltp=1007.0)  # above the 1,006 trigger, below the 1,008.05 limit
        assert isinstance(d, EntryDecision)
        assert (d.entry_trigger, d.entry_price) == (None, Decimal("1008.05"))

    def test_never_chase_an_extended_breakout(self, config):
        r = _decide(config, ltp=1008.05)
        assert isinstance(r, Rejection) and r.rule == "breakout_extended"

    def test_orb_only_before_11(self, config):
        r = _decide(config, minutes_since_market_open=120.0)
        assert isinstance(r, Rejection) and r.rule == "no_entry_strategy"


class TestVwap:
    def test_pullback_is_a_limit_at_vwap(self, config):
        d = _decide(config, price_vs_open_pct=0.6)  # ran up 0.6% from the open
        assert isinstance(d, EntryDecision)
        assert d.strategy == EntryStrategy.ID2
        assert (d.entry_trigger, d.entry_price) == (None, Decimal("998.00"))
        assert d.stop_price == Decimal("968.00")  # 998 − 30


class TestRejections:
    @pytest.mark.parametrize("ltp", [None, 0.0])
    def test_no_live_price(self, config, ltp):
        assert _decide(config, ltp=ltp).rule == "no_live_price"

    def test_no_atr(self, config):
        assert _decide(config, atr_14d=None).rule == "atr_unavailable"

    def test_bearish_signal_not_traded(self, config):
        r = _decide(
            config,
            premarket_gap_pct=-2.0,
            overnight_news_sentiment=-1.0,
            fo_oi_overnight_change_pct=-10.0,
            nifty_intraday_direction=-1.0,
            orb_range_vs_atr=1.9,
            minutes_since_latest_news=500.0,
        )
        assert r.rule == "signal_not_long"
        assert r.signal is not None  # still persisted for audit

    def test_liquidity_gate_from_the_plan(self, config):
        r = _decide(config, avg_traded_value_20d=5e7)  # ₹5 cr < ₹10 cr floor
        assert r.rule == "plan_liquidity_gate"

    def test_too_small_after_cap(self, config):
        r = _decide(config, capital="10000")  # cap 500 / 1,008 = 0 shares
        assert r.rule == "position_too_small"

    def test_regime_scale_shrinks_risk_size(self, config):
        d = _decide(config, capital="100000", regime="bear")  # 500/30=16 ×0.3=4; cap 4
        assert d.quantity == 4
