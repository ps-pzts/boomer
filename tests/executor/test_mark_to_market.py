"""Open positions get a live price and unrealised P&L from reconcile() (#106).

Worked numbers (qty 10, entry 100, stop 98, target 104):
  price 101.5 -> (101.5 - 100) x 10 = +15
  price  99.0 -> (99.0  - 100) x 10 = -10
  short, qty -10, entry 100, price 98 -> (98 - 100) x -10 = +20
"""

from __future__ import annotations

import sqlite3

import pytest

from alerts import commands
from db.migrations import run_migrations
from executor.position_manager import PositionManager
from executor.trade_lifecycle import TradeSupervisor
from tests.executor.test_trade_lifecycle import _MIGRATIONS, Harness, _filled_and_protected


@pytest.fixture()
def h(tmp_path) -> Harness:
    return Harness(tmp_path)


def _with_price(h: Harness, prices: dict) -> TradeSupervisor:
    return TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock, price=prices.get)


def test_open_position_is_marked_to_market(h):
    bid = _filled_and_protected(h)
    _with_price(h, {"INFY": 101.5}).reconcile()
    p = h.position(bid)
    assert (p["current_price"], p["unrealised_pnl"]) == (101.5, 15.0)


def test_loss_is_negative(h):
    bid = _filled_and_protected(h)
    _with_price(h, {"INFY": 99.0}).reconcile()
    assert h.position(bid)["unrealised_pnl"] == -10.0


def test_symbol_without_tick_is_left_unchanged(h):
    bid = _filled_and_protected(h)
    _with_price(h, {"INFY": 101.5}).reconcile()
    _with_price(h, {}).reconcile()  # no tick now: keep the last mark, do not zero it
    p = h.position(bid)
    assert (p["current_price"], p["unrealised_pnl"]) == (101.5, 15.0)


def test_no_price_callable_changes_nothing(h):
    bid = _filled_and_protected(h)
    h.sup.reconcile()
    p = h.position(bid)
    assert (p["current_price"], p["unrealised_pnl"]) == (100.0, 0.0)


def test_failing_price_source_does_not_break_reconcile(h):
    bid = _filled_and_protected(h)

    def boom(_symbol):
        raise RuntimeError("feed down")

    TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock, price=boom).reconcile()
    assert h.bracket(bid)["state"] == "open"


def test_closed_position_is_never_overwritten(h):
    bid = _filled_and_protected(h)
    h.tick(104.2)  # target fills: realised +40
    sup = _with_price(h, {"INFY": 150.0})
    sup.reconcile()
    sup.reconcile()
    p = h.position(bid)
    assert (p["is_open"], p["realised_pnl"], p["unrealised_pnl"]) == (0, 40.0, 0.0)
    assert p["current_price"] != 150.0


def test_short_position_sign(tmp_path):
    path = str(tmp_path / "s.db")
    run_migrations(path, _MIGRATIONS)
    db = sqlite3.connect(path)
    pm = PositionManager(db, gtt_manager=None, order_manager=None)
    pid = pm.open_position(
        symbol="INFY", exchange="NSE", track="intraday", bucket_id="intraday",
        broker_id="kite", quantity=-10, average_entry_price=100.0, stop_loss_price=102.0,
        target_price=96.0, atr_at_entry=2.0, entry_order_id="o",
    )  # fmt: skip
    pm.update_ltp("INFY", 98.0)
    row = db.execute("SELECT unrealised_pnl FROM positions WHERE position_id=?", (pid,))
    assert row.fetchone()[0] == 20.0


def test_telegram_positions_and_pnl_read_live_numbers(h):
    _filled_and_protected(h)
    _with_price(h, {"INFY": 101.5}).reconcile()
    db_path = h.db.execute("PRAGMA database_list").fetchone()[2]
    assert "now 101.50, +₹15" in commands.run(db_path, "/positions")
    pnl = commands.run(db_path, "/pnl")
    assert "Unrealised: +₹15 on 1 open" in pnl and "Total: +₹15" in pnl
