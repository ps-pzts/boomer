"""09:15 Kite order canary (#85): place, modify, cancel two orders that can't fill.

Worked example (LTP 1503.37, tick 0.10, offset 3%):
  1503.37 x 0.97 = 1458.2689
  SL-M SELL trigger rounded UP   -> 1458.30, modified 5 ticks away -> 1457.80
  LIMIT BUY price  rounded DOWN  -> 1458.20, modified 5 ticks away -> 1457.70
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import kiteconnect
import pytest

from executor.canary import TAG_PREFIX, CanaryConfig, run_canary
from tests.kitesim_kit import SimKite

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 19, 9, 15, 5, tzinfo=IST)


@pytest.fixture()
def fk() -> SimKite:
    """The canary on the real KiteBroker against KiteSim (#86)."""
    kite = SimKite(ltp=1503.37)
    yield kite
    kite.sdk.verify()


def _run(fk: SimKite, tmp_path, **cfg):
    config = CanaryConfig(recordings_dir=str(tmp_path / "rec"), **cfg)
    return run_canary(fk.broker(), config, clock=lambda: NOW, sleep=lambda s: None)


def _names(report) -> dict:
    return {r.name: r for r in report.results}


def _orders(fk: SimKite, side: str) -> list[dict]:
    return fk.side(side)


class TestHappyPath:
    def test_passes_and_leaves_nothing(self, fk, tmp_path):
        report = _run(fk, tmp_path)
        assert report.ok, report.failures()
        assert report.kind == "canary"
        assert [o["status"] for o in fk.book.values()] == ["CANCELLED", "CANCELLED"]
        assert all(o["filled_quantity"] == 0 for o in fk.book.values())

    def test_order_parameters(self, fk, tmp_path):
        _run(fk, tmp_path)
        place = {c["transaction_type"]: c for c in fk.sdk.called("place_order")}
        sl, lim = place["SELL"], place["BUY"]
        assert (sl["order_type"], sl["product"], sl["quantity"]) == ("SL-M", "MIS", 1)
        assert (lim["order_type"], lim["product"], lim["quantity"]) == ("LIMIT", "MIS", 1)
        assert sl["trigger_price"] == pytest.approx(1458.3)
        assert lim["price"] == pytest.approx(1458.2)
        for kw in (sl, lim):
            assert kw["tag"].startswith(TAG_PREFIX) and kw["tag"].isalnum()
            assert len(kw["tag"]) <= 20

    def test_modified_further_away_then_cancelled(self, fk, tmp_path):
        _run(fk, tmp_path)
        assert _orders(fk, "SELL")[0]["trigger_price"] == pytest.approx(1457.8)
        assert _orders(fk, "BUY")[0]["price"] == pytest.approx(1457.7)
        assert len(fk.sdk.called("modify_order")) == 2
        assert len(fk.sdk.called("cancel_order")) == 2

    def test_every_raw_response_is_recorded(self, fk, tmp_path):
        report = _run(fk, tmp_path)
        path = tmp_path / "rec" / "2026-11-19" / "canary-091505.json"
        assert report.extra["recording"] == str(path)
        calls = [r["call"] for r in json.loads(path.read_text())["responses"]]
        for call in ("ltp", "place_order", "modify_order", "cancel_order", "orders"):
            assert call in calls
        assert calls.count("place_order") == 2

    def test_never_touches_the_order_manager_or_db(self, fk, tmp_path):
        """Canary orders are not trades: only the broker is involved (no db arg)."""
        assert _run(fk, tmp_path).ok


class TestFailures:
    def test_rejected_order_fails_and_cancels_the_other(self, fk, tmp_path):
        fk.script.reject = {"SELL"}
        report = _run(fk, tmp_path)
        assert not report.ok
        assert any("REJECTED" in f for f in report.failures())
        assert _orders(fk, "BUY")[0]["status"] == "CANCELLED"
        assert _names(report)["cleanup"].ok

    def test_unexpected_fill_fails(self, fk, tmp_path):
        fk.script.fill = {"BUY"}
        report = _run(fk, tmp_path)
        assert not report.ok
        assert any("filled" in f for f in report.failures())
        assert _orders(fk, "SELL")[0]["status"] == "CANCELLED"  # the other one cleaned up

    def test_cancel_failure_reports_the_resting_orders(self, fk, tmp_path):
        fk.script.raises["cancel_order"] = kiteconnect.exceptions.InputException(
            "Order cannot be cancelled"
        )
        report = _run(fk, tmp_path)
        assert not report.ok
        cleanup = _names(report)["cleanup"]
        assert not cleanup.ok
        for oid in fk.book:
            assert oid in cleanup.detail
        # cancel tried by the step, then again by cleanup for every resting order
        assert len(fk.sdk.called("cancel_order")) >= 3

    def test_ltp_drift_near_a_canary_price_cancels_at_once(self, fk, tmp_path):
        fk.script.ltp_script = [1503.37, 1465.0]  # within 1% of the 1458.3 trigger
        report = _run(fk, tmp_path)
        assert not report.ok
        assert any("ltp_drift" in f for f in report.failures())
        assert fk.sdk.called("modify_order") == []
        assert [o["status"] for o in fk.book.values()] == ["CANCELLED", "CANCELLED"]

    def test_place_exception_fails_and_cleans_up(self, fk, tmp_path):
        fk.script.raises["place_order"] = kiteconnect.exceptions.InputException(
            "Markets are closed"
        )
        report = _run(fk, tmp_path)
        assert not report.ok and "Markets are closed" in " ".join(report.failures())
        assert _names(report)["cleanup"].ok

    def test_leftover_canary_order_from_an_earlier_run_is_cancelled(self, fk, tmp_path):
        old = fk.sim.place_order(
            "regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1400.0, tag="CNRYOLD"
        )
        fk.script.reject = {"SELL"}
        _run(fk, tmp_path)
        assert fk.book[old]["status"] == "CANCELLED"

    def test_ltp_unavailable_places_nothing(self, fk, tmp_path):
        fk.script.raises["ltp"] = kiteconnect.exceptions.NetworkException("timeout")
        report = _run(fk, tmp_path)
        assert not report.ok
        assert fk.sdk.called("place_order") == []

    def test_order_never_rests_times_out(self, fk, tmp_path):
        fk.script.stuck = "VALIDATION PENDING"
        report = _run(fk, tmp_path, max_polls=3)
        assert not report.ok and any("timeout" in f for f in report.failures())

    def test_unauthenticated_broker_fails(self, tmp_path):
        from executor.brokers.kite_broker import KiteBroker

        cfg = CanaryConfig(recordings_dir=str(tmp_path))
        report = run_canary(KiteBroker(), cfg, clock=lambda: NOW, sleep=lambda s: None)
        assert not report.ok
