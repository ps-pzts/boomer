"""Daily Kite pre-flight (#85): read-only checks against the real KiteBroker."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from zoneinfo import ZoneInfo

import kiteconnect
import pytest

from executor.brokers.feeds.base import FeedHealth
from executor.preflight import CheckReport, CheckResult, run_preflight
from tests.executor.kite_fake import FakeKite

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 19, 8, 55, tzinfo=IST)


class _Feed:
    def __init__(self, connected: bool) -> None:
        self.connected = connected
        self.calls = 0

    def health(self) -> FeedHealth:
        self.calls += 1
        return FeedHealth(connected=self.connected, last_tick_at=None, subscribed=1)


def _run(fk: FakeKite, feed=None) -> CheckReport:
    return run_preflight(fk.broker(), feed, clock=lambda: NOW, sleep=lambda s: None)


def _by_name(report: CheckReport) -> dict[str, CheckResult]:
    return {r.name: r for r in report.results}


def test_happy_path_every_check_passes():
    report = _run(FakeKite(), _Feed(True))
    assert report.ok, report.failures()
    assert report.kind == "preflight"
    assert list(_by_name(report)) == [
        "token",
        "funds",
        "instruments",
        "orders",
        "positions",
        "ltp",
        "feed",
    ]
    assert report.ran_at == NOW.replace(tzinfo=None).isoformat()


def test_report_is_frozen_and_serialisable():
    report = _run(FakeKite(), _Feed(True))
    with pytest.raises(FrozenInstanceError):
        report.kind = "x"  # type: ignore[misc]
    d = report.as_dict()
    assert d["ok"] is True and d["checks"][0] == {"name": "token", "ok": True, "detail": "AB1234"}


def test_invalid_token_fails_and_skips_the_rest():
    fk = FakeKite()
    fk.sdk.profile.side_effect = kiteconnect.exceptions.TokenException("Incorrect api_key")
    report = _run(fk, _Feed(True))
    r = _by_name(report)
    assert not report.ok
    assert not r["token"].ok and "TokenException" in r["token"].detail
    assert all(not r[n].ok and "token invalid" in r[n].detail for n in ("funds", "orders"))
    fk.sdk.orders.assert_not_called()  # nothing else hits Kite with a dead token


def test_shape_mismatch_fails_the_check_with_the_problem():
    fk = FakeKite()
    fk.sdk.margins.side_effect = lambda segment=None: {
        "available": {"live_balance": 1.0},  # Kite renamed 'cash'
        "utilised": {"debits": 0.0},
    }
    r = _by_name(_run(fk, _Feed(True)))
    assert not r["funds"].ok and "available.cash" in r["funds"].detail
    assert r["token"].ok and r["orders"].ok


def test_order_shape_checked_for_every_order():
    fk = FakeKite()
    oid = fk._place("regular", "NSE", "INFY", "BUY", 1, "MIS", "LIMIT", price=1.0)
    del fk.book[oid]["filled_quantity"]
    r = _by_name(_run(fk, _Feed(True)))
    assert not r["orders"].ok and "filled_quantity" in r["orders"].detail


def test_unknown_probe_instrument_fails():
    fk = FakeKite()
    fk.sdk.instruments.side_effect = lambda exchange=None: []
    r = _by_name(_run(fk, _Feed(True)))
    assert not r["instruments"].ok and "NSE:INFY" in r["instruments"].detail


def test_feed_not_connected_fails_after_waiting():
    feed = _Feed(False)
    r = _by_name(_run(FakeKite(), feed))
    assert not r["feed"].ok and feed.calls > 1


def test_no_feed_in_this_process_is_skipped_not_failed():
    r = _by_name(_run(FakeKite(), None))
    assert r["feed"].ok and r["feed"].detail.startswith("skipped")


def test_unauthenticated_broker_fails_token():
    from executor.brokers.kite_broker import KiteBroker

    report = run_preflight(KiteBroker(), None, clock=lambda: NOW, sleep=lambda s: None)
    assert not report.ok and not _by_name(report)["token"].ok
