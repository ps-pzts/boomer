"""KiteSim speaks kiteconnect (#86): the same signatures, the constants KiteBroker
reads, and only payload shapes Kite documents (conformance (a))."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from kiteconnect import KiteConnect

import executor.brokers.kite_broker as kite_broker_module
import executor.brokers.kite_gtt as kite_gtt_module
from executor.brokers.kite_contract import check_shape
from executor.brokers.kitesim import KiteSim
from executor.models import GttRequest, GttType, OrderRequest, OrderSide, OrderType, ProductType
from tests import kite_fixtures as kf
from tests.kitesim_kit import make_sim, paper_broker

_SRC = inspect.getsource(kite_broker_module) + inspect.getsource(kite_gtt_module)
_CALLED = set(re.findall(r"_kite\.([a-z_]+)\(", _SRC)) | set(re.findall(r"\bk\.([a-z_]+)\(", _SRC))
_CONSTANTS = set(re.findall(r"kite\.([A-Z_]+)\b", _SRC))
API = sorted(
    n
    for n, v in vars(KiteSim).items()
    if callable(v) and not n.startswith("_") and hasattr(KiteConnect, n)
)


def test_kite_broker_uses_only_what_the_simulator_implements():
    assert _CALLED, "found no SDK calls in kite_broker.py: the regex is stale"
    assert set(API) >= _CALLED, _CALLED - set(API)
    for name in _CONSTANTS:
        assert getattr(KiteSim, name) == getattr(KiteConnect, name)


@pytest.mark.parametrize("name", API)
def test_signature_matches_kiteconnect_exactly(name):
    assert inspect.signature(getattr(KiteSim, name)) == inspect.signature(
        getattr(KiteConnect, name)
    )


def test_every_sdk_constant_is_there():
    for name in dir(KiteConnect):
        if name.isupper():
            assert getattr(KiteSim, name) == getattr(KiteConnect, name)


def _req(side, kind, qty=10, price=0.0, trigger=0.0, sym="INFY"):
    return OrderRequest(sym, "NSE", side, kind, qty, ProductType.MIS, price, trigger)


@pytest.fixture()
def busy_sim():
    """Orders in every status Kite reports at rest, a position, a GTT."""
    sim = make_sim(cash=200_000, liquidity_per_tick=5)
    b = paper_broker(sim)
    sim.on_price("INFY", "NSE", 1500.0)
    sim.on_price("KPEL", "NSE", 360.0)
    b.place_order(_req(OrderSide.BUY, OrderType.LIMIT, qty=10, price=1500.0))  # part filled
    b.place_order(_req(OrderSide.SELL, OrderType.SL, trigger=1450.0))  # TRIGGER PENDING
    gone = b.place_order(_req(OrderSide.BUY, OrderType.LIMIT, price=1400.0))
    b.cancel_order(gone)  # CANCELLED
    b.place_order(_req(OrderSide.BUY, OrderType.LIMIT, qty=1000, price=1490.0))  # REJECTED
    b.place_order(_req(OrderSide.BUY, OrderType.MARKET, qty=5, sym="KPEL"))  # COMPLETE
    b.place_gtt(GttRequest("KPEL", "NSE", GttType.OCO, 5, sl_trigger_price=338.05,
                           sl_limit_price=337.35, target_trigger_price=439.45,
                           target_limit_price=438.6))  # fmt: skip
    return sim


class TestShapes:
    def test_orders_in_every_status(self, busy_sim):
        orders = busy_sim.orders()
        assert {o["status"] for o in orders} == {
            "OPEN",
            "TRIGGER PENDING",
            "CANCELLED",
            "REJECTED",
            "COMPLETE",
        }
        assert check_shape("orders", orders) == []
        for o in orders:
            assert check_shape("orders", busy_sim.order_history(o["order_id"])) == []

    def test_every_kind_the_simulator_returns(self, busy_sim):
        s = busy_sim
        assert check_shape("positions", s.positions()) == []
        assert check_shape("holdings", s.holdings()) == []
        assert check_shape("margins_equity", s.margins(segment="equity")) == []
        assert check_shape("ltp", s.ltp(["NSE:INFY", "NSE:KPEL"])) == []
        assert check_shape("ltp", s.ltp("NSE:INFY")) == []
        assert check_shape("instruments", s.instruments(exchange="NSE")) == []
        assert check_shape("gtts", s.get_gtts()) == []
        gid = s.get_gtts()[0]["id"]
        assert check_shape("gtt", s.get_gtt(gid)) == []
        assert check_shape("historical", s.historical_data(408065, "a", "b", "day")) == []
        oid = s.place_order("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1400.0)
        assert check_shape("place_order", oid) == []
        assert check_shape("place_order", s.modify_order("regular", oid, price=1400.5)) == []
        assert check_shape("place_order", s.cancel_order("regular", oid)) == []
        ack = s.delete_gtt(gid)
        assert check_shape("gtt_ack", ack) == []

    def test_postbacks(self):
        sim = make_sim(liquidity_per_tick=5)
        got: list[dict] = []
        sim.subscribe_postbacks(got.append)
        b = paper_broker(sim)
        sim.on_price("INFY", "NSE", 1500.0)
        b.place_order(_req(OrderSide.BUY, OrderType.LIMIT, qty=10, price=1500.0))
        assert len(got) >= 2
        for p in got:
            assert check_shape("order_postback", p) == []

    @pytest.mark.parametrize(
        ("kind", "fixture"),
        [
            ("orders", lambda: kf.sdk("orders")[0]),
            ("postback", kf.postback),
            ("position", lambda: kf.sdk("positions")["net"][0]),
            ("margins", lambda: kf.sdk("margins")),
            ("gtt", lambda: kf.sdk("get_gtt")),
        ],
    )
    def test_carries_every_key_the_documented_payload_has(self, busy_sim, kind, fixture):
        """Beyond the contract's required keys: nothing a real payload has is missing."""
        sim_payload = {
            "orders": lambda: busy_sim.orders()[0],
            "postback": lambda: _one_postback(),
            "position": lambda: busy_sim.positions()["net"][0],
            "margins": lambda: busy_sim.margins(segment="equity"),
            "gtt": lambda: busy_sim.get_gtts()[0],
        }[kind]()
        assert _keys(fixture()) <= _keys(sim_payload)

    def test_profile(self):
        p = make_sim().profile()
        assert p["user_id"] == "AB1234" and p["broker"] == "ZERODHA"


def _one_postback() -> dict:
    sim = make_sim()
    got: list[dict] = []
    sim.subscribe_postbacks(got.append)
    sim.place_order("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1400.0)
    return got[0]


def _keys(d: dict, prefix: str = "") -> set[str]:
    out = set()
    for k, v in d.items():
        out.add(prefix + k)
        if isinstance(v, dict) and k not in ("meta",):
            out |= _keys(v, f"{prefix}{k}.")
    return out


def test_fixture_dir_is_present():
    assert (Path(kf.FIXTURES) / "orders.json").exists()
