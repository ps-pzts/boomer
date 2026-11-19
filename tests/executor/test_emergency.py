"""executor/emergency.py — flatten untracked broker positions on emergency stop (#67)."""

from __future__ import annotations

import sqlite3
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from executor.emergency import flatten_untracked
from executor.models import OrderSide, OrderStatus, OrderType, ProductType

DAY = date(2026, 11, 19)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE orders (order_id TEXT, idempotency_key TEXT)")
    return c


def _pos(sym="TCS", qty=5, product="mis", exchange="NSE"):
    return SimpleNamespace(symbol=sym, quantity=qty, product=product, exchange=exchange)


def _om(conn, status=OrderStatus.PENDING):
    om = MagicMock()

    def submit(req, track):
        oid = f"o-{req.idempotency_key}"
        conn.execute("INSERT INTO orders VALUES (?, ?)", (oid, req.idempotency_key))
        return oid

    om.submit.side_effect = submit
    om.sync_status.return_value = SimpleNamespace(status=status)
    return om


def test_long_sold_short_bought_at_market(conn):
    om = _om(conn)
    acts = flatten_untracked(conn, om, [_pos("TCS", 5), _pos("SBIN", -3)], set(), DAY)
    reqs = [c.args[0] for c in om.submit.call_args_list]
    assert [(r.symbol, r.side, r.quantity) for r in reqs] == [
        ("TCS", OrderSide.SELL, 5),
        ("SBIN", OrderSide.BUY, 3),
    ]
    assert all(r.order_type == OrderType.MARKET and r.product == ProductType.MIS for r in reqs)
    assert [a["action"] for a in acts] == ["exit_placed", "exit_placed"]


def test_never_two_exits_in_flight(conn):
    om = _om(conn, status=OrderStatus.PENDING)
    flatten_untracked(conn, om, [_pos()], set(), DAY)
    acts = flatten_untracked(conn, om, [_pos()], set(), DAY)  # first still working
    assert om.submit.call_count == 1
    assert acts[0]["action"] == "exit_in_flight"


def test_next_exit_after_previous_is_terminal(conn):
    """A rejected exit doesn't leave the position stranded: the next tick retries."""
    om = _om(conn, status=OrderStatus.REJECTED)
    flatten_untracked(conn, om, [_pos()], set(), DAY)
    flatten_untracked(conn, om, [_pos()], set(), DAY)
    keys = [c.args[0].idempotency_key for c in om.submit.call_args_list]
    assert keys == ["emergency-2026-11-19-TCS-1", "emergency-2026-11-19-TCS-2"]


def test_symbol_prefix_is_exact(conn):
    """'TCS' must not see 'TCSX' orders as its own (no LIKE wildcards)."""
    om = _om(conn)
    flatten_untracked(conn, om, [_pos("TCSX")], set(), DAY)
    acts = flatten_untracked(conn, om, [_pos("TCS")], set(), DAY)
    assert acts[0]["action"] == "exit_placed"


def test_tracked_symbols_and_non_mis_skipped(conn):
    om = _om(conn)
    acts = flatten_untracked(
        conn, om, [_pos("INFY"), _pos("HDFC", product="cnc"), _pos("ITC", qty=0)], {"INFY"}, DAY
    )
    om.submit.assert_not_called()
    assert acts == [{"symbol": "INFY", "net": 5, "action": "skipped_tracked"}]


def test_one_refusal_does_not_stop_the_rest(conn):
    om = _om(conn)
    ok = om.submit.side_effect

    def submit(req, track):
        if req.symbol == "TCS":
            raise RuntimeError("outside market hours")
        return ok(req, track)

    om.submit.side_effect = submit
    acts = flatten_untracked(conn, om, [_pos("TCS"), _pos("SBIN")], set(), DAY)
    assert [a["action"] for a in acts] == ["exit_failed", "exit_placed"]
