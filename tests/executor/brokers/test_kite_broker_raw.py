"""KiteBroker raw read-only access and response recording (#85).

Pre-flight and canary need Kite's raw payloads (to validate their shape and to
record them as fixtures) without calling the SDK outside executor/brokers/.
"""

from __future__ import annotations

import pytest

from executor.brokers.kite_contract import check_shape
from executor.models import OrderRequest, OrderSide, OrderType, ProductType
from tests.executor.kite_fake import INFY_ROW, FakeKite


@pytest.fixture()
def fk() -> FakeKite:
    return FakeKite()


class TestRawRead:
    def test_each_read_only_endpoint(self, fk):
        b = fk.broker()
        assert b.raw_read("profile")["user_id"] == "AB1234"
        assert b.raw_read("margins_equity")["available"]["cash"] == 100000.0
        fk.sdk.margins.assert_called_with(segment="equity")
        assert b.raw_read("orders") == []
        assert b.raw_read("positions") == {"net": [], "day": []}
        assert b.raw_read("ltp", "NSE:INFY")["NSE:INFY"]["last_price"] == 1500.0

    def test_write_endpoints_are_refused(self, fk):
        with pytest.raises(ValueError, match="not a read-only"):
            fk.broker().raw_read("place_order")

    def test_needs_authentication(self):
        from executor.brokers.kite_broker import KiteBroker

        with pytest.raises(RuntimeError, match="not authenticated"):
            KiteBroker().raw_read("profile")

    def test_instrument_row_from_the_cached_dump(self, fk):
        b = fk.broker()
        assert b.instruments.row("INFY", "NSE") == INFY_ROW
        assert b.instruments.row("NOPE", "NSE") is None
        assert fk.sdk.instruments.call_count == 1  # loaded once


class TestRecorder:
    def test_place_modify_cancel_responses_are_recorded(self, fk):
        b = fk.broker()
        seen: list[tuple] = []
        b.set_response_recorder(lambda call, params, resp: seen.append((call, params, resp)))
        oid = b.place_order(
            OrderRequest(
                symbol="INFY",
                exchange="NSE",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=1,
                product=ProductType.MIS,
                price=1455.0,
                tag="CNRY1",
            )
        )
        b.modify_order(oid, {"price": 1454.5})
        b.cancel_order(oid)
        assert [c for c, _, _ in seen] == ["place_order", "modify_order", "cancel_order"]
        assert seen[0][1]["tag"] == "CNRY1" and seen[0][2] == oid
        assert seen[1][1] == {"order_id": oid, "price": 1454.5}

    def test_no_recorder_by_default(self, fk):
        b = fk.broker()
        b.cancel_order(fk._place("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1.0))

    def test_recorder_failure_never_breaks_an_order(self, fk):
        b = fk.broker()
        b.set_response_recorder(lambda *a: 1 / 0)
        oid = fk._place("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1.0)
        b.cancel_order(oid)
        assert fk.book[oid]["status"] == "CANCELLED"


class TestContractStandIn:
    """Minimal stand-in; replaced by the full kite_contract module at integration."""

    def test_fake_payloads_match(self, fk):
        b = fk.broker()
        assert check_shape("margins_equity", b.raw_read("margins_equity")) == []
        assert check_shape("positions", b.raw_read("positions")) == []
        assert check_shape("ltp", b.raw_read("ltp", "NSE:INFY")) == []
        assert check_shape("instrument", INFY_ROW) == []
        oid = fk._place("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1.0)
        assert check_shape("order", fk.book[oid]) == []

    def test_missing_key_reported(self):
        problems = check_shape("margins_equity", {"available": {}, "utilised": {"debits": 0}})
        assert problems and "cash" in problems[0]

    def test_wrong_container_reported(self):
        assert check_shape("positions", []) != []
        assert check_shape("ltp", {"NSE:INFY": {"instrument_token": 1}}) != []

    def test_unknown_kind(self):
        assert check_shape("nope", {}) == ["unknown shape kind: nope"]
