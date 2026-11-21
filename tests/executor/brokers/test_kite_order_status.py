"""#73: Kite order statuses -> ours, end to end through OrderManager.

Before the fix, any status missing from the map (MODIFY PENDING, VALIDATION
PENDING, ...) became a terminal ERROR: the Trade machine read that as a lost stop
and exited a healthy trade at market. A triggered stop-limit entry (Kite: TRIGGER
PENDING -> OPEN) also hit an illegal TRIGGERED -> PENDING transition.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db.migrations import run_migrations
from executor.brokers.kite_broker import KiteBroker, map_kite_status
from executor.brokers.kite_contract import IN_FLIGHT_ORDER_STATUSES
from executor.models import (
    BrokerName,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from executor.order_manager import OrderManager
from tests import kite_fixtures as kf
from tests.executor.test_trade_lifecycle import _Clock
from tests.kite_sdk import respond

S = OrderStatus
_MIGRATIONS = Path(__file__).parents[3] / "migrations"


class TestMap:
    @pytest.mark.parametrize(
        "status, order_type, filled, qty, expected",
        [
            ("COMPLETE", "LIMIT", 10, 10, S.FILLED),
            ("CANCELLED", "SL-M", 0, 10, S.CANCELLED),
            ("CANCELLED AMO", "LIMIT", 0, 10, S.CANCELLED),
            ("REJECTED", "SL-M", 0, 10, S.REJECTED),
            ("TRIGGER PENDING", "SL-M", 0, 10, S.PENDING),  # resting stop
            ("TRIGGER PENDING", "SL", 0, 10, S.PENDING),
            ("OPEN", "LIMIT", 0, 10, S.PENDING),
            ("OPEN", "SL", 0, 10, S.TRIGGERED),  # stop-limit after its trigger
            ("OPEN", "LIMIT", 4, 10, S.PARTIAL),
            ("OPEN", "SL", 4, 10, S.PARTIAL),
        ],
    )
    def test_known(self, status, order_type, filled, qty, expected):
        assert map_kite_status(status, order_type, filled, qty) == expected

    @pytest.mark.parametrize(
        "status",
        [
            "PUT ORDER REQ RECEIVED",
            "VALIDATION PENDING",
            "OPEN PENDING",
            "MODIFY VALIDATION PENDING",
            "MODIFY PENDING",
            "MODIFIED",
            "CANCEL PENDING",
            "AMO REQ RECEIVED",
            "SOMETHING NEW",
        ],
    )
    def test_in_flight_and_unknown_are_no_change_never_error(self, status):
        assert map_kite_status(status, "SL-M", 0, 10) is None

    def test_one_share_filled_is_already_partial(self):
        assert map_kite_status("OPEN", "LIMIT", 1, 10) == S.PARTIAL
        assert map_kite_status("OPEN", "LIMIT", 10, 10) == S.PENDING  # all filled: not partial

    @pytest.mark.parametrize("status", sorted(IN_FLIGHT_ORDER_STATUSES))
    def test_in_flight_is_silent(self, status, caplog):
        with caplog.at_level("WARNING"):
            map_kite_status(status, "SL-M", 0, 10)
        assert caplog.records == []

    def test_unknown_status_is_logged_once(self, caplog):
        with caplog.at_level("WARNING"):
            assert map_kite_status("SOMETHING NEW", "SL-M", 0, 10) is None
        assert [r.getMessage() for r in caplog.records] == [
            "kite_unknown_order_status status='SOMETHING NEW' (treated as unchanged)"
        ]


_PLACED = kf.sdk("place_order")  # the id Kite gives the order we place


class _Kite:
    """Scripted kite.orders(): each call returns the next status of our order,
    built from the documented TRIGGER PENDING SL-M order in orders.json."""

    def __init__(self, order_type: str, script: list[tuple[str, int]]):
        self.order_type, self.script = order_type, list(script)

    def orders(self):
        status, filled = self.script.pop(0) if len(self.script) > 1 else self.script[0]

        def ours(wire: list[dict]) -> list[dict]:
            done = status in ("COMPLETE", "CANCELLED", "REJECTED")
            return [
                {
                    **wire[1],
                    "order_id": _PLACED,
                    "status": status,
                    "order_type": self.order_type,
                    "quantity": 10,
                    "filled_quantity": filled,
                    "pending_quantity": 0 if done else 10 - filled,
                    "average_price": 100.0 if filled else 0,
                }
            ]

        return kf.sdk("orders", transform=ours)


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    p = str(tmp_path / "k.db")
    run_migrations(p, _MIGRATIONS)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c


def _om(db, kite: _Kite, sdk) -> tuple[OrderManager, str]:
    broker = KiteBroker()
    broker._kite = sdk
    respond(sdk, "orders", kite.orders)
    for method in ("place_order", "modify_order", "cancel_order"):
        respond(sdk, method, _PLACED)
    respond(sdk, "margins", kf.sdk("margins"))
    respond(sdk, "instruments", kf.sdk("instruments"))
    clock = _Clock()
    om = OrderManager(db, {BrokerName.KITE: broker}, ltp_cache={}, clock=clock)
    om._sleep = lambda s: None
    kind = OrderType.SL if kite.order_type == "SL-M" else OrderType.SL_LIMIT
    side = OrderSide.SELL if kite.order_type == "SL-M" else OrderSide.BUY
    oid = om.submit(
        OrderRequest(
            symbol="INFY",
            exchange="NSE",
            side=side,
            order_type=kind,
            quantity=10,
            product=ProductType.MIS,
            price=0.0 if kind == OrderType.SL else 101.0,
            trigger_price=98.0 if kind == OrderType.SL else 100.5,
            idempotency_key="k1",
        ),
        "intraday",
    )
    return om, oid


def test_stop_modify_in_flight_keeps_the_stop_resting(db, kite_sdk):
    """Trailing-stop modify: TRIGGER PENDING -> MODIFY PENDING -> TRIGGER PENDING."""
    om, oid = _om(
        db,
        _Kite("SL-M", [("TRIGGER PENDING", 0), ("MODIFY PENDING", 0), ("TRIGGER PENDING", 0)]),
        kite_sdk,
    )
    assert [om.sync_status(oid).status for _ in range(3)] == [S.PENDING] * 3


def test_stop_limit_entry_triggers_then_fills(db, kite_sdk):
    om, oid = _om(
        db,
        _Kite("SL", [("TRIGGER PENDING", 0), ("OPEN", 0), ("OPEN", 4), ("COMPLETE", 10)]),
        kite_sdk,
    )
    seen = [om.sync_status(oid) for _ in range(4)]
    assert [r.status for r in seen] == [S.PENDING, S.TRIGGERED, S.PARTIAL, S.FILLED]
    assert [r.filled_quantity for r in seen] == [0, 0, 4, 10]


def test_partial_fill_recorded_without_a_status_change(db, kite_sdk):
    om, oid = _om(db, _Kite("SL", [("OPEN", 0), ("OPEN", 4), ("OPEN", 6)]), kite_sdk)
    om.sync_status(oid)
    om.sync_status(oid)
    r = om.sync_status(oid)
    assert (r.status, r.filled_quantity) == (S.PARTIAL, 6)
    qty = db.execute("SELECT SUM(quantity) FROM executions WHERE order_id=?", (oid,)).fetchone()[0]
    assert qty == 6


def test_cancel_waits_for_kite_and_records_a_racing_fill(db, kite_sdk):
    """CANCEL PENDING, then the stop actually COMPLETEs: must be FILLED, not CANCELLED."""
    om, oid = _om(
        db,
        _Kite("SL-M", [("TRIGGER PENDING", 0), ("CANCEL PENDING", 0), ("COMPLETE", 10)]),
        kite_sdk,
    )
    om.sync_status(oid)
    r = om.cancel(oid)
    assert (r.status, r.filled_quantity) == (S.FILLED, 10)


def test_cancel_still_in_flight_is_not_marked_cancelled(db, kite_sdk):
    om, oid = _om(db, _Kite("SL-M", [("TRIGGER PENDING", 0), ("CANCEL PENDING", 0)]), kite_sdk)
    om.sync_status(oid)
    assert om.cancel(oid).status == S.PENDING  # decided by a later sync, not assumed


def test_place_order_returns_the_sdk_id_string(kite_sdk):
    """kiteconnect.place_order returns the id itself; the old code indexed it (#73)."""
    broker = KiteBroker()
    broker._kite = kite_sdk
    respond(kite_sdk, "place_order", kf.sdk("place_order"))
    req = OrderRequest(
        symbol="INFY",
        exchange="NSE",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        product=ProductType.MIS,
        price=100.0,
        idempotency_key="8a66f9e6-aca9-4b06-b49d-ca0c05aa5ec7-entry",
    )
    assert broker.place_order(req) == "240611000123401"
    tag = broker._kite.place_order.call_args.kwargs["tag"]
    assert tag.isalnum() and len(tag) == 20
    # #97: the tag identifies the key. Truncating the UUID gave every order of one trade
    # (entry, stop, target, exits) the same tag.
    from executor.brokers.kite_broker import _kite_tag

    uuid = "8a66f9e6-aca9-4b06-b49d-ca0c05aa5ec7"
    assert len({_kite_tag(f"{uuid}-{leg}") for leg in ("entry", "stop", "target", "exit1")}) == 4
    assert _kite_tag("short-key") == "shortkey"  # fits in 20: kept readable
