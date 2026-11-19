"""GTTs on the paper KiteBroker + KiteSim (#86; ported from the pre-#86 simulator's GTT tests).

GTTs now carry Kite's own shape (get_gtt: `status` "active" / "triggered" /
"deleted", the fired leg's `result`), not the pre-#86 simulator's GttStatus enum and
`triggered_leg` key.
"""

from __future__ import annotations

import kiteconnect
import pytest

from executor.brokers.kite_contract import check_shape
from executor.models import GttRequest, GttType, PriceBar
from tests.kitesim_kit import make_sim, paper_broker


@pytest.fixture()
def sim():
    return make_sim()


@pytest.fixture()
def broker(sim):
    b = paper_broker(sim)
    yield b
    b._kite.verify()


def single(symbol: str, trigger: float, limit: float, qty: int = 10) -> GttRequest:
    return GttRequest(symbol, "NSE", GttType.SINGLE, qty, trigger_price=trigger, limit_price=limit)


def oco(symbol="ONGC", qty=50, sl=150.0, sl_limit=148.0, target=200.0, target_limit=202.0):
    return GttRequest(
        symbol, "NSE", GttType.OCO, qty,
        sl_trigger_price=sl, sl_limit_price=sl_limit,
        target_trigger_price=target, target_limit_price=target_limit,
    )  # fmt: skip


def _fired_leg(gtt: dict) -> int | None:
    return next((i for i, o in enumerate(gtt["orders"]) if o["result"]), None)


class TestPlacement:
    def test_place_single_gtt(self, sim, broker):
        sim.on_price("TATA", "NSE", 520.0)
        gtt = broker.get_gtt(broker.place_gtt(single("TATA", 500.0, 498.0)))
        assert gtt["status"] == "active"
        assert gtt["condition"]["trigger_values"] == [500.0]
        assert gtt["condition"]["last_price"] == 520.0
        assert check_shape("gtt", gtt) == []

    def test_needs_a_last_price(self, broker):
        with pytest.raises(RuntimeError, match="LTP unavailable"):
            broker.place_gtt(single("TATA", 500.0, 498.0))

    def test_oco_stop_above_the_market_is_refused(self, sim, broker):
        sim.on_price("ONGC", "NSE", 149.0)
        with pytest.raises(kiteconnect.exceptions.InputException, match="stoploss trigger"):
            broker.place_gtt(oco())

    def test_off_tick_leg_price_is_refused(self, sim, broker):
        sim.on_price("INFY", "NSE", 1500.0)  # INFY tick 0.10
        with pytest.raises(kiteconnect.exceptions.InputException, match="Tick size"):
            broker.place_gtt(single("INFY", 1450.0, 1449.95))

    def test_list_gtts(self, sim, broker):
        sim.on_price("X", "NSE", 400.0)
        for price in [100.0, 200.0, 300.0]:
            broker.place_gtt(single("X", price, price - 1))
        gtts = broker.list_gtts()
        assert len(gtts) == 3 and check_shape("gtts", gtts) == []


class TestTriggers:
    def test_single_gtt_triggers_when_price_hits(self, sim, broker):
        sim.on_price("SAIL", "NSE", 92.0)
        gid = broker.place_gtt(single("SAIL", 90.0, 89.0, qty=100))
        sim.on_bar(PriceBar("SAIL", "2024-01-02", open=92, high=93, low=88, close=91, volume=1))
        gtt = broker.get_gtt(gid)
        assert gtt["status"] == "triggered"
        leg = gtt["orders"][0]["result"]
        assert leg["order_result"]["status"] == "success"
        (order,) = sim.orders()
        assert order["order_id"] == leg["order_result"]["order_id"]
        assert (order["transaction_type"], order["order_type"], order["price"]) == (
            "SELL",
            "LIMIT",
            89.0,
        )

    def test_oco_gtt_triggers_sl_leg(self, sim, broker):
        sim.on_price("ONGC", "NSE", 160.0)
        gid = broker.place_gtt(oco())
        sim.on_bar(PriceBar("ONGC", "2024-01-02", 155, 158, 148, 152, 1_000_000))
        gtt = broker.get_gtt(gid)
        assert gtt["status"] == "triggered" and _fired_leg(gtt) == 0

    def test_oco_gtt_triggers_target_leg(self, sim, broker):
        sim.on_price("ONGC", "NSE", 160.0)
        gid = broker.place_gtt(oco())
        sim.on_bar(PriceBar("ONGC", "2024-01-02", 195, 205, 192, 198, 1_000_000))
        gtt = broker.get_gtt(gid)
        assert gtt["status"] == "triggered" and _fired_leg(gtt) == 1

    def test_bar_touching_both_legs_fires_the_one_nearer_the_open(self, sim, broker):
        """The pre-#86 simulator always fired the stop; KiteSim walks the bar and fires the leg
        the price reaches first (open 199 is nearer the high)."""
        sim.on_price("ONGC", "NSE", 160.0)
        gid = broker.place_gtt(oco())
        sim.on_bar(PriceBar("ONGC", "2024-01-02", 199, 205, 145, 160, 1))
        assert _fired_leg(broker.get_gtt(gid)) == 1

    def test_triggered_gtt_fires_once(self, sim, broker):
        sim.on_price("SAIL", "NSE", 92.0)
        broker.place_gtt(single("SAIL", 90.0, 89.0))
        sim.on_price("SAIL", "NSE", 89.5)
        sim.on_price("SAIL", "NSE", 92.0)
        sim.on_price("SAIL", "NSE", 89.0)
        assert len(sim.orders()) == 1


class TestModifyAndCancel:
    def test_cancel_gtt(self, sim, broker):
        sim.on_price("SBI", "NSE", 520.0)
        gid = broker.place_gtt(single("SBI", 500.0, 498.0, qty=100))
        broker.cancel_gtt(gid)
        assert broker.get_gtt(gid)["status"] == "deleted"

    def test_modify_moves_only_the_named_leg(self, sim, broker):
        """#84: Kite's modify_gtt replaces the whole trigger; the stop leg trails,
        the target leg is resent unchanged."""
        sim.on_price("ONGC", "NSE", 170.0)
        gid = broker.place_gtt(oco())
        broker.modify_gtt(gid, {"leg1": {"stopPrice": 160.0, "limitPrice": 159.0}})
        gtt = broker.get_gtt(gid)
        assert gtt["condition"]["trigger_values"] == [160.0, 200.0]
        assert [o["price"] for o in gtt["orders"]] == [159.0, 202.0]
        assert gtt["condition"]["last_price"] == 170.0

    def test_triggered_gtt_cannot_be_modified(self, sim, broker):
        sim.on_price("ONGC", "NSE", 160.0)
        gid = broker.place_gtt(oco())
        sim.on_price("ONGC", "NSE", 149.0)
        with pytest.raises(kiteconnect.exceptions.InputException, match="triggered"):
            broker.modify_gtt(gid, {"leg1": {"stopPrice": 140.0, "limitPrice": 139.0}})

    def test_unknown_gtt(self, broker):
        with pytest.raises(kiteconnect.exceptions.InputException):
            broker.get_gtt("424242")
