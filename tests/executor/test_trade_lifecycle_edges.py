"""TradeSupervisor edge cases found by mutation testing (#89).

Same harness and numbers as test_trade_lifecycle.py (qty 10, entry 100, stop 98,
target 104, ATR 2): target hit = +40, square-off at 101 = +10.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from executor.brokers.kitesim.config import KiteSimConfig
from executor.brokers.kitesim.paper import build_paper_broker
from executor.models import OrderStatus
from executor.order_manager import OrderManager
from executor.position_manager import PositionManager
from executor.trade_lifecycle import TradeSupervisor, validate
from tests.executor.test_trade_lifecycle import Harness, _filled_and_protected, _req
from tests.kitesim_kit import fixture_instruments


@pytest.fixture()
def h(tmp_path) -> Harness:
    return Harness(tmp_path)


def _orders(h, where="1=1", args=()):
    return h.db.execute(f"SELECT * FROM orders WHERE {where}", args).fetchall()  # noqa: S608


class TestValidateEdges:
    def test_request_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            _req().quantity = 5  # type: ignore[misc]

    def test_one_share_is_a_trade(self):
        assert validate(_req(qty=1)) is None
        assert validate(_req(qty=0)) == "quantity_below_one"

    def test_sub_rupee_prices_are_valid(self):
        assert validate(_req(entry=1.0, stop=0.5, target=2.0)) is None

    def test_atr_must_be_positive_but_may_be_small(self):
        base = _req()
        assert validate(dataclasses.replace(base, atr=0.0)) == "atr_not_positive"
        assert validate(dataclasses.replace(base, atr=-1.0)) == "atr_not_positive"
        assert validate(dataclasses.replace(base, atr=0.5)) is None


class TestEntryOrder:
    def test_limit_entry_carries_no_trigger(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req())
        (row,) = _orders(h, "idempotency_key=?", (f"{bid}-entry",))
        assert (row["order_type"], row["price"], row["trigger_price"]) == ("limit", 100.0, 0.0)

    def test_stop_and_target_legs_carry_the_right_prices(self, h):
        bid = _filled_and_protected(h)
        b = h.bracket(bid)
        stop = h.om.sync_status(b["stop_order_id"])
        target = h.om.sync_status(b["target_order_id"])
        assert (stop.price, stop.trigger_price) == (0.0, 98.0)
        assert (target.price, target.trigger_price) == (104.0, 0.0)


def _partial_harness(tmp_path, per_tick: int) -> Harness:
    h = Harness(tmp_path)
    h.paper = build_paper_broker(
        h.feed,
        1_000_000,
        instruments=fixture_instruments(),
        clock=h.clock,
        config=KiteSimConfig(liquidity_per_tick=per_tick),
    )
    h.om = OrderManager(h.db, {h.om._brokers.popitem()[0]: h.paper}, ltp_cache={}, clock=h.clock)
    h.pm = PositionManager(h.db, order_manager=h.om)
    h.sup = TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock)
    return h


class TestPartialEntry:
    def test_a_single_share_partial_fill_is_protected(self, tmp_path):
        # 1 of 10 shares fills: shares exist, so cancel the rest and protect that one share
        h = _partial_harness(tmp_path, per_tick=1)
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["filled_quantity"]) == ("open", 1)
        stop = h.om.sync_status(b["stop_order_id"])
        assert (stop.quantity, stop.status) == (1, OrderStatus.PENDING)
        assert h.om.sync_status(b["entry_order_id"]).status == OrderStatus.CANCELLED

    def test_square_off_keeps_a_single_share_partial(self, tmp_path):
        h = _partial_harness(tmp_path, per_tick=1)
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.sup.square_off_all()
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["filled_quantity"]) == ("closed", 1)
        assert h.paper.list_positions() == []


class TestSquareOffDetails:
    def test_exit_is_a_priceless_market_order_with_the_first_exit_key(self, h):
        bid = _filled_and_protected(h)
        h.tick(101.0)
        h.sup.square_off_all()
        h.sup.reconcile()
        (exit_row,) = _orders(h, "idempotency_key LIKE ?", (f"{bid}-exit%",))
        assert exit_row["idempotency_key"] == f"{bid}-exit1"
        assert (exit_row["order_type"], exit_row["price"], exit_row["trigger_price"]) == (
            "market",
            0.0,
            0.0,
        )
        assert (exit_row["side"], exit_row["quantity"]) == ("sell", 10)

    def test_next_exit_after_a_failed_one_uses_the_next_key(self, h):
        bid = _filled_and_protected(h)
        with patch.object(h.sup._store, "exit_attempts", return_value=1):
            h.tick(101.0)
            h.sup.square_off_all()
        assert _orders(h, "idempotency_key=?", (f"{bid}-exit2",))

    def test_square_off_with_no_target_order_does_not_error(self, h):
        bid = _filled_and_protected(h)
        h.db.execute("UPDATE intraday_brackets SET target_order_id=NULL WHERE bracket_id=?", (bid,))
        h.db.commit()
        h.tick(101.0)
        h.sup.square_off_all()
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "closed"
        h.alerter.critical.assert_not_called()

    def test_closed_at_is_the_clock_time(self, h):
        bid = _filled_and_protected(h)
        h.tick(104.2)
        h.sup.reconcile()
        assert h.bracket(bid)["closed_at"] == h.clock.now.isoformat()

    def test_expired_entry_records_when_it_closed(self, h):
        h.tick(100.5)
        bid = h.sup.open_trade(_req(entry=100.0))
        h.clock.now += timedelta(minutes=30)
        h.sup.reconcile()
        assert h.bracket(bid)["closed_at"] == h.clock.now.isoformat()


class TestOpenLegs:
    def test_both_legs_resting(self, h):
        bid = _filled_and_protected(h)
        b = h.bracket(bid)
        assert h.sup.open_legs() == [
            (bid, "INFY", "stop", b["stop_order_id"], True),
            (bid, "INFY", "target", b["target_order_id"], True),
        ]

    def test_a_leg_the_broker_no_longer_shows_working_is_not_resting(self, h):
        bid = _filled_and_protected(h)
        b = h.bracket(bid)
        stop = h.om.sync_status(b["stop_order_id"])
        h.paper.cancel_order(stop.broker_order_id)
        legs = {leg: (oid, resting) for _, _, leg, oid, resting in h.sup.open_legs()}
        assert legs["stop"] == (b["stop_order_id"], False)
        assert legs["target"] == (b["target_order_id"], True)

    def test_no_target_means_no_target_leg(self, h):
        bid = _filled_and_protected(h)
        h.db.execute("UPDATE intraday_brackets SET target_order_id=NULL WHERE bracket_id=?", (bid,))
        h.db.commit()
        b = h.bracket(bid)
        assert h.sup.open_legs() == [(bid, "INFY", "stop", b["stop_order_id"], True)]

    def test_a_missing_stop_is_reported_not_resting(self, h):
        bid = _filled_and_protected(h)
        h.db.execute("UPDATE intraday_brackets SET stop_order_id=NULL WHERE bracket_id=?", (bid,))
        h.db.commit()
        legs = h.sup.open_legs()
        assert (bid, "INFY", "stop", None, False) in legs

    def test_only_open_trades_have_legs(self, h):
        h.tick(100.5)
        h.sup.open_trade(_req(entry=100.0))  # still ENTRY_PENDING
        assert h.sup.open_legs() == []


class TestProtectingEdges:
    def test_missing_target_warns_but_keeps_the_stop(self, h):
        real = h.sup._place_leg
        h.sup._place_leg = lambda b, leg: None if leg == "target" else real(b, leg)
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "open"
        assert h.bracket(bid)["target_order_id"] is None
        h.alerter.warn.assert_called_once()
        assert h.alerter.warn.call_args.args[0] == "Target not placed — INFY"

    def test_target_placed_means_no_warning(self, h):
        _filled_and_protected(h)
        h.alerter.warn.assert_not_called()

    def test_stop_already_filled_at_first_look_closes_the_trade(self, h):
        # the broker reports the freshly placed stop FILLED (10 @ 98): -20 and done
        real = h.om.sync_status

        def sync(order_id):
            rec = real(order_id)
            if rec.order_type == "sl":
                return dataclasses.replace(
                    rec, status=OrderStatus.FILLED, filled_quantity=10, average_fill_price=98.0
                )
            return rec

        h.om.sync_status = sync
        h.tick(100.0)
        bid = h.sup.open_trade(_req())
        h.sup.reconcile()
        b = h.bracket(bid)
        assert (b["state"], b["exit_reason"], b["realised_pnl"]) == ("closed", "stop_hit", -20.0)
        rows = h.db.execute(
            "SELECT payload FROM events WHERE entity_id=? AND kind='state_transition'", (bid,)
        ).fetchall()
        assert any("stop_filled_immediately" in r["payload"] for r in rows)


class TestOcoAndNoTrailer:
    def test_only_the_resting_leg_is_cancelled_on_a_target_hit(self, h):
        bid = _filled_and_protected(h)
        b = h.bracket(bid)
        h.tick(104.2)
        with patch.object(h.om, "cancel", wraps=h.om.cancel) as cancel:
            h.sup.reconcile()
        cancelled = [c.args[0] for c in cancel.call_args_list]
        assert b["stop_order_id"] in cancelled
        assert b["target_order_id"] not in cancelled  # it filled; nothing to cancel

    def test_watching_an_open_trade_without_a_price_feed_raises_nothing(self, h):
        _filled_and_protected(h)  # the harness supervisor has no trailer
        h.tick(100.8)
        h.sup.reconcile()
        h.sup.reconcile()
        h.alerter.critical.assert_not_called()

    def test_stop_lost_without_a_target_still_exits(self, h):
        bid = _filled_and_protected(h)
        h.db.execute("UPDATE intraday_brackets SET target_order_id=NULL WHERE bracket_id=?", (bid,))
        h.db.commit()
        stop = h.om.sync_status(h.bracket(bid)["stop_order_id"])
        h.paper.cancel_order(stop.broker_order_id)
        h.tick(100.5)
        h.sup.reconcile()
        h.sup.reconcile()
        assert h.bracket(bid)["state"] == "closed"
        assert h.critical_titles() == ["UNPROTECTED INFY — exiting at market"]


class TestSettlement:
    def test_exit_price_is_entry_plus_pnl_per_share(self, h):
        bid = _filled_and_protected(h)
        h.tick(104.2)
        with patch.object(h.pm, "close_position", wraps=h.pm.close_position) as close:
            h.sup.reconcile()
        pid = h.bracket(bid)["position_id"]
        close.assert_called_once_with(pid, 104.0, 40.0)  # 100 + 40 / 10

    def test_gap_loss_exit_price(self, h):
        bid = _filled_and_protected(h)
        h.tick(97.5)
        with patch.object(h.pm, "close_position", wraps=h.pm.close_position) as close:
            h.sup.reconcile()
        close.assert_called_once_with(h.bracket(bid)["position_id"], 97.5, -25.0)

    def test_close_is_reported_to_the_operator(self, h):
        _filled_and_protected(h)
        h.tick(104.2)
        h.sup.reconcile()
        h.alerter.info.assert_called_once()
        title, body = h.alerter.info.call_args.args
        assert title == "Trade closed — INFY"
        assert body == "target_hit, realised P&L ₹40.00"
        assert h.alerter.info.call_args.kwargs == {"source_task_id": "trade_supervisor"}

    def test_without_an_alerter_everything_still_works(self, h):
        sup = TradeSupervisor(h.db, h.om, h.pm, alerter=None, clock=h.clock)
        h.tick(100.0)
        bid = sup.open_trade(_req())
        sup.reconcile()
        h.tick(104.2)
        sup.reconcile()
        assert h.bracket(bid)["state"] == "closed"
        sup._warn("t", "b")  # no alerter: silently fine
        sup._info("t", "b")
        sup._critical("t", "b")

    def test_a_single_share_long_is_sold_not_bought(self, h):
        # net +1: the corrective order must SELL 1. (net > 1 would have bought instead)
        bid = _filled_and_protected(h)
        h.sup._net_and_cash = lambda b: (1, 0.0)
        h.sup._cancel_resting_legs = lambda b: []
        sent = {}
        h.sup._submit = lambda symbol, ex, side, kind, qty, key, **kw: (
            sent.update(side=side.value, qty=qty, key=key) or "oid-x"
        )
        with patch.object(h.sup._store, "advance") as advance:
            h.sup._settle(h.bracket(bid))
        assert sent == {"side": "sell", "qty": 1, "key": f"{bid}-exit1"}
        assert advance.call_args.kwargs["severity"] == "info"
        assert json.loads(advance.call_args.kwargs["exit_order_ids"]) == ["oid-x"]
        assert advance.call_args.kwargs["inputs"]["net"] == 1

    def test_a_single_share_oversold_is_bought_back_critically(self, h):
        bid = _filled_and_protected(h)
        h.sup._net_and_cash = lambda b: (-1, 0.0)
        h.sup._cancel_resting_legs = lambda b: []
        sent = {}
        h.sup._submit = lambda symbol, ex, side, kind, qty, key, **kw: (
            sent.update(side=side.value, qty=qty) or "oid-y"
        )
        with patch.object(h.sup._store, "advance") as advance:
            h.sup._settle(h.bracket(bid))
        assert sent == {"side": "buy", "qty": 1}
        assert advance.call_args.kwargs["severity"] == "critical"
        assert h.critical_titles()[-1] == "Oversold INFY by 1 — buying back"
