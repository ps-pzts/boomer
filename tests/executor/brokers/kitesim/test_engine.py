"""KiteSim's engine (#86): partial fills, accounting, state, determinism, threads,
and the hostile-mode hooks (#87) — interceptor, advance, postback outbox."""

from __future__ import annotations

import json
import threading

import kiteconnect
import pytest

from executor.brokers.kitesim.engine import EOD_MESSAGE
from executor.models import OrderRequest, OrderSide, OrderStatus, OrderType, ProductType
from tests.kitesim_kit import Clock, make_sim, paper_broker


def req(qty=10, kind=OrderType.LIMIT, price=100.0, trigger=0.0, side=OrderSide.BUY):
    return OrderRequest("INFY", "NSE", side, kind, qty, ProductType.MIS, price, trigger)


class TestPartialFills:
    def test_liquidity_per_tick_fills_in_parts(self):
        sim = make_sim(liquidity_per_tick=4)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 101.0)
        oid = b.place_order(req(qty=10, price=100.0))
        sim.on_price("INFY", "NSE", 100.0)
        st = b.get_order_status(oid)
        assert (st["status"], st["filled_quantity"]) == (OrderStatus.PARTIAL, 4)
        (o,) = sim.orders()
        assert (o["status"], o["pending_quantity"]) == ("OPEN", 6)
        sim.on_price("INFY", "NSE", 99.9)
        sim.on_price("INFY", "NSE", 99.8)
        st = b.get_order_status(oid)
        assert (st["status"], st["filled_quantity"]) == (OrderStatus.FILLED, 10)
        assert st["average_fill_price"] == 100.0

    def test_cancel_after_a_partial_fill_keeps_the_filled_part(self):
        sim = make_sim(liquidity_per_tick=3)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 100.0)
        oid = b.place_order(req(qty=10, price=100.0))  # 3 filled at once
        b.cancel_order(oid)
        (o,) = sim.orders()
        assert (o["status"], o["filled_quantity"], o["cancelled_quantity"]) == ("CANCELLED", 3, 7)
        (pos,) = b.list_positions()
        assert pos.quantity == 3


class TestAccounting:
    def test_positions_pnl_and_margins(self):
        """Buy 10 @100, sell 10 @104: realised +40; margins carry it (m2m_realised)."""
        sim = make_sim(cash=100_000)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 100.0)
        b.place_order(req(price=100.0))
        sim.on_price("INFY", "NSE", 102.0)
        (row,) = sim.positions()["net"]
        assert (row["unrealised"], row["m2m"], row["value"]) == (20.0, 20.0, -1000.0)
        assert sim.margins(segment="equity")["utilised"]["debits"] == pytest.approx(200.0)
        b.place_order(req(price=104.0, side=OrderSide.SELL))
        sim.on_price("INFY", "NSE", 104.0)
        (row,) = sim.positions()["net"]
        assert (row["quantity"], row["realised"], row["pnl"]) == (0, 40.0, 40.0)
        m = sim.margins(segment="equity")
        assert (m["utilised"]["debits"], m["utilised"]["m2m_realised"]) == (0.0, 40.0)
        assert sim.margins()["equity"] == m

    def test_losses_reduce_the_available_margin(self):
        sim = make_sim(cash=10_000)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 100.0)
        b.place_order(req(price=100.0))
        sim.on_price("INFY", "NSE", 97.0)
        b.place_order(req(kind=OrderType.MARKET, price=0.0, side=OrderSide.SELL))
        assert sim.margins(segment="equity")["net"] == pytest.approx(10_000 - 30.0)


class TestEndOfDay:
    def test_working_day_orders_expire_and_close_becomes_reference(self):
        sim = make_sim(circuit_pct=0.10)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 1500.0)
        oid = b.place_order(req(price=1400.0))
        sim.on_price("INFY", "NSE", 1600.0)
        sim.end_of_day()
        (o,) = sim.orders()
        assert (o["status"], o["status_message"]) == ("CANCELLED", EOD_MESSAGE)
        assert b.get_order_status(oid)["status"] == OrderStatus.CANCELLED
        # next day's band is around 1600: 1450 is now outside it
        assert b.get_order_status(b.place_order(req(price=1430.0)))["status"] == "rejected"


class TestState:
    def test_round_trip_is_json_safe_and_complete(self):
        sim = make_sim()
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 100.0)
        b.place_order(req(price=100.0))
        stop = b.place_order(req(kind=OrderType.SL, trigger=98.0, side=OrderSide.SELL, price=0))
        state = json.loads(json.dumps(sim.to_state()))
        twin = make_sim()
        assert twin.load_state(state)
        assert twin.orders() == sim.orders()
        assert twin.positions() == sim.positions()
        assert twin.margins(segment="equity") == sim.margins(segment="equity")
        twin.on_price("INFY", "NSE", 97.0)  # the restored stop still fires
        assert {o["order_id"]: o["status"] for o in twin.orders()}[stop] == "COMPLETE"
        # ids continue, never reused
        new = paper_broker(twin).place_order(req(price=96.0))
        assert new not in {o["order_id"] for o in sim.orders()}

    def test_mockbroker_era_book_is_ignored(self, caplog):
        sim = make_sim()
        assert sim.load_state({"cash": 5.0, "orders": {}, "positions": {}}) is False
        assert "kitesim_state_ignored" in caplog.text
        assert sim.load_state(None) is False

    def test_on_change_fires_once_per_call_that_changed_something(self):
        sim = make_sim()
        saved: list[dict] = []
        sim.on_change = saved.append
        sim.on_price("INFY", "NSE", 100.0)
        assert saved == []  # a price alone changes no order
        paper_broker(sim).place_order(req(price=100.0))  # accepted + filled: one save
        assert len(saved) == 1 and saved[0]["seq"] == 1


class TestDeterminism:
    def test_same_seed_same_ids(self):
        def run() -> list[dict]:
            sim = make_sim(seed=7)
            b = paper_broker(sim)
            sim.on_price("INFY", "NSE", 100.0)
            b.place_order(req(price=100.0))
            return sim.orders()

        assert run() == run()

    def test_time_comes_only_from_the_clock(self):
        clock = Clock()
        sim = make_sim(clock=clock)
        paper_broker(sim).place_order(req(price=99.0))
        (o,) = sim.orders()
        assert o["order_timestamp"] == clock.now.replace(tzinfo=None)


class TestHooks:
    def test_interceptor_sees_every_call_and_can_rewrite(self):
        sim = make_sim()
        seen: list[str] = []

        def spy(call, params, proceed):
            seen.append(call)
            return proceed()

        sim.interceptor = spy
        b = paper_broker(sim)
        b.place_order(req(price=99.0))
        b.get_funds()
        assert seen == ["place_order", "margins"]

    def test_interceptor_can_raise_a_kite_exception(self):
        sim = make_sim()

        def down(call, params, proceed):
            raise kiteconnect.exceptions.NetworkException("Gateway timed out.")

        sim.interceptor = down
        with pytest.raises(kiteconnect.exceptions.NetworkException):
            sim.orders()

    def test_advance_forces_a_state_change_and_books_fills(self):
        sim = make_sim()
        b = paper_broker(sim)
        got: list[dict] = []
        b.on_order_update(got.append)
        oid = b.place_order(req(price=99.0))
        sim.advance(oid, "COMPLETE", filled_quantity=10, pending_quantity=0, average_price=99.5)
        assert got[-1]["status"] == "COMPLETE"
        (pos,) = b.list_positions()
        assert (pos.quantity, pos.average_price) == (10, 99.5)

    def test_postbacks_are_delivered_outside_the_lock(self):
        """A listener that calls the simulator from another thread must not deadlock
        (the feed thread delivers while a task thread waits on the lock)."""
        sim = make_sim()
        b = paper_broker(sim)
        seen: list[int] = []

        def listener(update):
            t = threading.Thread(target=lambda: seen.append(len(sim.orders())))
            t.start()
            t.join(timeout=2)

        b.on_order_update(listener)
        b.place_order(req(price=99.0))
        assert seen == [1]

    def test_concurrent_ticks_and_orders(self):
        sim = make_sim(cash=1e9)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 100.0)

        def ticks():
            for i in range(300):
                sim.on_price("INFY", "NSE", 100.0 + (i % 5) * 0.1)

        def orders():
            for _ in range(50):
                b.place_order(req(qty=1, price=100.2))

        threads = [threading.Thread(target=ticks), threading.Thread(target=orders)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(sim.orders()) == 50
        sim.on_price("INFY", "NSE", 100.0)  # every resting buy is now reached
        assert all(o["status"] == "COMPLETE" for o in sim.orders())
        filled = sum(o["filled_quantity"] for o in sim.orders())
        assert sim.positions()["net"][0]["quantity"] == filled == 50
