"""
Regression for the server-local date bug: collector default dates must be the IST date.

Worked example: 2025-03-10 20:00 UTC is 2025-03-11 01:30 IST, so a UTC server's
date.today() is a day behind the IST trading date.
"""

import sqlite3
from datetime import UTC, date, datetime

import pytest

from collector import base
from collector.fetchers.bulk_deals import BseBulkDealsFetcher
from collector.fetchers.fo_oi import FoOiFetcher
from collector.fetchers.prices import NsePricesFetcher, prune_old_prices
from collector.fetchers.shares_outstanding import SharesOutstandingFetcher

_UTC_INSTANT = datetime(2025, 3, 10, 20, 0, tzinfo=UTC)
_IST_DAY = date(2025, 3, 11)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return _UTC_INSTANT.astimezone(tz)

    monkeypatch.setattr(base, "datetime", _Frozen)


def test_ist_today_is_ist_date_not_utc_date():
    assert base.ist_today() == _IST_DAY


@pytest.mark.parametrize(
    "fetcher", [NsePricesFetcher, FoOiFetcher, BseBulkDealsFetcher, SharesOutstandingFetcher]
)
def test_default_fetch_url_uses_ist_date(fetcher):
    assert fetcher.fetch_url(None) == fetcher.fetch_url(None, trade_date=_IST_DAY)


def test_explicit_trade_date_unchanged():
    other = date(2025, 1, 5)
    assert NsePricesFetcher.fetch_url(None, trade_date=other) != NsePricesFetcher.fetch_url(None)


def test_prune_cutoff_is_ist_based():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE prices (trade_date TEXT)")
    db.executemany("INSERT INTO prices VALUES (?)", [("2025-03-09",), ("2025-03-10",)])
    # IST today 03-11, keep 1 day -> cutoff 03-10: only 03-09 is older (a UTC date would keep it).
    assert prune_old_prices(db, keep_days=1) == 1
