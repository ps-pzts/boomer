"""Tests for brain.features.runner — FeatureComputer + run_track_computers."""

from datetime import date
from pathlib import Path

import pytest

from brain.feature_store import FeatureStore
from brain.features.runner import FeatureComputer, run_track_computers
from db.migrations import run_migrations

_MIGRATIONS = Path(__file__).parents[3] / "migrations"

AS_OF = date(2024, 6, 1)
SYM, EXC = "TCS", "NSE"


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    run_migrations(str(p), _MIGRATIONS)
    return str(p)


@pytest.fixture
def fs(db_path):
    return FeatureStore(db_path)


# ── FeatureComputer descriptor ────────────────────────────────────────────────

def test_feature_computer_is_frozen():
    fc = FeatureComputer(fn=None, writes=("a",))
    with pytest.raises((AttributeError, TypeError)):
        fc.essential = False  # type: ignore[misc]


def test_feature_computer_defaults():
    fc = FeatureComputer(fn=lambda *a: None, writes=("x", "y"))
    assert fc.essential is True
    assert fc.requires_live is False


# ── run_track_computers ───────────────────────────────────────────────────────

def test_runs_batch_computer_and_reports_success(db_path, fs):
    called = []

    def my_fn(db_path, fs, symbol, exchange, as_of):
        called.append(symbol)

    registry = [FeatureComputer(fn=my_fn, writes=("feat_a",))]
    results = run_track_computers(registry, db_path, fs, SYM, EXC, AS_OF)

    assert called == [SYM]
    assert results["feat_a"] is True


def test_skips_live_only_computer(db_path, fs):
    called = []

    def my_fn(db_path, fs, symbol, exchange, as_of):
        called.append(symbol)

    registry = [
        FeatureComputer(fn=None, writes=("live_feat",), requires_live=True),
        FeatureComputer(fn=my_fn, writes=("batch_feat",)),
    ]
    results = run_track_computers(registry, db_path, fs, SYM, EXC, AS_OF)

    assert called == [SYM]
    assert "live_feat" not in results
    assert results["batch_feat"] is True


def test_non_essential_failure_continues(db_path, fs):
    called = []

    def boom(db_path, fs, symbol, exchange, as_of):
        raise RuntimeError("expected test error")

    def ok(db_path, fs, symbol, exchange, as_of):
        called.append("ok")

    registry = [
        FeatureComputer(fn=boom, writes=("bad_feat",), essential=False),
        FeatureComputer(fn=ok, writes=("good_feat",)),
    ]
    results = run_track_computers(registry, db_path, fs, SYM, EXC, AS_OF)

    assert called == ["ok"]
    assert results["bad_feat"] is False
    assert results["good_feat"] is True


def test_essential_failure_raises(db_path, fs):
    def boom(db_path, fs, symbol, exchange, as_of):
        raise ValueError("essential boom")

    registry = [FeatureComputer(fn=boom, writes=("critical_feat",), essential=True)]

    with pytest.raises(ValueError, match="essential boom"):
        run_track_computers(registry, db_path, fs, SYM, EXC, AS_OF)


def test_registry_executes_in_order(db_path, fs):
    order = []

    def first(db_path, fs, symbol, exchange, as_of):
        order.append("first")

    def second(db_path, fs, symbol, exchange, as_of):
        order.append("second")

    registry = [
        FeatureComputer(fn=first, writes=("f1",)),
        FeatureComputer(fn=second, writes=("f2",)),
    ]
    run_track_computers(registry, db_path, fs, SYM, EXC, AS_OF)
    assert order == ["first", "second"]


# ── Registry import smoke tests (no DB needed) ───────────────────────────────

def test_intraday_registry_importable():
    from brain.features.track_intraday import INTRADAY_COMPUTERS
    assert len(INTRADAY_COMPUTERS) > 0
    assert all(isinstance(c, FeatureComputer) for c in INTRADAY_COMPUTERS)


def test_swing_registry_importable():
    from brain.features.track_swing import SWING_COMPUTERS
    assert len(SWING_COMPUTERS) > 0
    assert all(isinstance(c, FeatureComputer) for c in SWING_COMPUTERS)


def test_long_term_registry_importable():
    from brain.features.track_long_term import LONG_TERM_COMPUTERS
    assert len(LONG_TERM_COMPUTERS) > 0
    assert all(isinstance(c, FeatureComputer) for c in LONG_TERM_COMPUTERS)


def test_live_only_computers_have_none_fn():
    from brain.features.track_intraday import INTRADAY_COMPUTERS
    for c in INTRADAY_COMPUTERS:
        if c.requires_live:
            assert c.fn is None, f"{c.writes} is requires_live but fn is not None"


def test_swing_price_features_is_first():
    from brain.features.track_swing import SWING_COMPUTERS
    first = SWING_COMPUTERS[0]
    assert "price_close" in first.writes, "price_features must be first in swing registry"
