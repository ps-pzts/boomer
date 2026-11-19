"""Tests for KiteBroker.get_ltp() fallback chain and place_gtt() LTP guard.

Regression coverage for the silent bad-fallback bug:
  place_gtt() used `get_ltp() or sl_trigger_price` which caused Kite to reject
  the order with "Trigger cannot be created with one of the trigger values equal
  to the last price." when get_ltp() returned None.

The SDK is an autospec'd KiteConnect fed documented responses (#84).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from executor.brokers.feeds.base import Tick
from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.kite_broker import KiteBroker
from executor.models import GttRequest, GttType
from tests import kite_fixtures as kf
from tests.kite_sdk import respond

IST = ZoneInfo("Asia/Kolkata")

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def broker(kite_sdk) -> KiteBroker:
    b = KiteBroker()
    b._kite = kite_sdk
    return b


def _ltp_down(broker: KiteBroker, holdings=None, positions=None) -> None:
    """REST LTP refused (no market-data plan); tier-2 sources as given."""
    respond(broker._kite, "ltp", kf.sdk_error("permission"))
    respond(broker._kite, "holdings", [] if holdings is None else holdings)
    respond(broker._kite, "positions", kf.no_positions() if positions is None else positions)


# ── get_ltp() fallback chain ──────────────────────────────────────────────────


class TestGetLtp:
    def test_tier1_rest_ltp_used_when_available(self, broker: KiteBroker) -> None:
        respond(broker._kite, "ltp", kf.sdk("ltp"))
        assert broker.get_ltp("INFY", "NSE") == 1510.15
        broker._kite.ltp.assert_called_once_with(["NSE:INFY"])

    def test_tier2_holdings_fallback_when_rest_fails(self, broker: KiteBroker) -> None:
        _ltp_down(broker, holdings=kf.sdk("holdings"))
        assert broker.get_ltp("KPEL", "NSE") == 375.0

    def test_tier2_positions_fallback_when_rest_and_holdings_miss(self, broker: KiteBroker) -> None:
        _ltp_down(broker, positions=kf.sdk("positions"))
        assert broker.get_ltp("INFY", "NSE") == 1510.15

    def test_tier3_stale_tick_cache_used_as_last_resort(self, broker: KiteBroker) -> None:
        _ltp_down(broker)
        broker._ltp["KPEL"] = 355.0  # stale but present
        assert broker.get_ltp("KPEL", "NSE") == 355.0

    def test_returns_none_when_all_tiers_exhausted(self, broker: KiteBroker) -> None:
        _ltp_down(broker)
        assert broker.get_ltp("KPEL", "NSE") is None

    def test_zero_last_price_in_holdings_is_skipped(self, broker: KiteBroker) -> None:
        """Holdings entries with last_price=0 (delisted / not yet priced) must be skipped."""
        unpriced = kf.sdk("holdings", transform=lambda d: [{**h, "last_price": 0} for h in d])
        _ltp_down(broker, holdings=unpriced)
        assert broker.get_ltp("KPEL", "NSE") is None


# ── place_gtt() LTP guard ─────────────────────────────────────────────────────


class TestPlaceGttLtpGuard:
    def _oco_request(self) -> GttRequest:
        return GttRequest(
            symbol="KPEL",
            exchange="NSE",
            gtt_type=GttType.OCO,
            quantity=18,
            sl_trigger_price=338.03,
            sl_limit_price=337.36,
            target_trigger_price=439.46,
            target_limit_price=438.58,
        )

    def test_raises_when_ltp_unavailable(self, broker: KiteBroker) -> None:
        """place_gtt must raise RuntimeError — not silently use sl_trigger as last_price."""
        _ltp_down(broker)

        with pytest.raises(RuntimeError, match="LTP unavailable"):
            broker.place_gtt(self._oco_request())

        broker._kite.place_gtt.assert_not_called()

    def test_oco_uses_ltp_from_rest(self, broker: KiteBroker) -> None:
        """place_gtt passes the real LTP — not the sl_trigger — to the Kite SDK."""
        respond(broker._kite, "ltp", kf.ltp("KPEL", 360.0))
        respond(broker._kite, "place_gtt", kf.sdk("place_gtt"))

        assert broker.place_gtt(self._oco_request()) == "123456"

        call_kwargs = broker._kite.place_gtt.call_args[1]
        assert call_kwargs["last_price"] == 360.0
        # Confirm the bad-fallback value (sl_trigger) was NOT used
        assert call_kwargs["last_price"] != 338.03

    def test_oco_uses_ltp_from_holdings_fallback(self, broker: KiteBroker) -> None:
        _ltp_down(broker, holdings=kf.sdk("holdings"))
        respond(broker._kite, "place_gtt", kf.sdk("place_gtt"))

        broker.place_gtt(self._oco_request())

        assert broker._kite.place_gtt.call_args[1]["last_price"] == 375.0

    def test_single_gtt_raises_when_ltp_unavailable(self, broker: KiteBroker) -> None:
        _ltp_down(broker)

        req = GttRequest(
            symbol="KPEL",
            exchange="NSE",
            gtt_type=GttType.SINGLE,
            quantity=18,
            trigger_price=338.03,
            limit_price=337.36,
        )
        with pytest.raises(RuntimeError, match="LTP unavailable"):
            broker.place_gtt(req)


# ── Feed delegation (KiteFeed owns the WebSocket since B7 PR 1) ──────────────


def _tick(symbol: str, ltp: float, age_seconds: float = 0.0) -> Tick:
    return Tick(
        symbol=symbol,
        exchange="NSE",
        ltp=ltp,
        received_at=datetime.now(IST) - timedelta(seconds=age_seconds),
    )


class TestFeedDelegation:
    def test_fresh_feed_tick_used_before_rest(self, broker: KiteBroker) -> None:
        feed = ManualFeed()
        broker.attach_feed(feed)
        feed.push(_tick("KPEL", 401.5))
        assert broker.get_ltp("KPEL", "NSE") == 401.5
        broker._kite.ltp.assert_not_called()

    def test_stale_feed_tick_falls_through_to_rest(self, broker: KiteBroker) -> None:
        feed = ManualFeed()
        broker.attach_feed(feed)
        feed.push(_tick("KPEL", 390.0, age_seconds=600))
        respond(broker._kite, "ltp", kf.ltp("KPEL", 402.0))
        assert broker.get_ltp("KPEL", "NSE") == 402.0

    def test_stale_feed_tick_is_last_resort(self, broker: KiteBroker) -> None:
        feed = ManualFeed()
        broker.attach_feed(feed)
        feed.push(_tick("KPEL", 390.0, age_seconds=600))
        _ltp_down(broker)
        assert broker.get_ltp("KPEL", "NSE") == 390.0

    def test_on_tick_subscribes_and_filters_to_requested_symbols(self, broker: KiteBroker) -> None:
        feed = ManualFeed()
        broker.attach_feed(feed)
        received: list[tuple[str, float]] = []
        broker.on_tick(["KPEL"], lambda sym, ltp: received.append((sym, ltp)))
        feed.push(_tick("KPEL", 401.5))
        feed.push(_tick("OTHER", 10.0))
        assert feed.subscribed == {"KPEL"}
        assert received == [("KPEL", 401.5)]

    def test_order_updates_come_from_feed(self, broker: KiteBroker) -> None:
        feed = ManualFeed()
        broker.attach_feed(feed)
        updates: list[dict] = []
        broker.on_order_update(updates.append)
        feed.push_order_update(kf.postback())
        assert updates == [kf.postback()]

    def test_on_tick_without_feed_raises(self, broker: KiteBroker) -> None:
        with pytest.raises(RuntimeError, match="no feed"):
            broker.on_tick(["KPEL"], lambda s, p: None)


# ── _resolve_instrument_token() caching ────────────────────────────────────────


class TestResolveInstrumentToken:
    def test_caches_per_exchange_not_globally(self, broker: KiteBroker) -> None:
        """Regression: a single global cache would silently misresolve or fail
        lookups for a second exchange once the first exchange's list was cached."""
        nse = kf.sdk("instruments")
        bse = [{**r, "exchange": "BSE", "instrument_token": r["instrument_token"] + 1} for r in nse]
        respond(broker._kite, "instruments", nse, bse)

        nse_token = broker._resolve_instrument_token("KPEL", "NSE")
        bse_token = broker._resolve_instrument_token("KPEL", "BSE")

        assert nse_token == 2955009
        assert bse_token == 2955010
        assert broker._kite.instruments.call_count == 2

    def test_second_lookup_same_exchange_uses_cache(self, broker: KiteBroker) -> None:
        respond(broker._kite, "instruments", kf.sdk("instruments"))

        broker._resolve_instrument_token("KPEL", "NSE")
        broker._resolve_instrument_token("KPEL", "NSE")

        assert broker._kite.instruments.call_count == 1


# ── place_order parameters (#43) ──────────────────────────────────────────────


def _order(order_type, price=0.0, trigger=0.0):
    from executor.models import OrderRequest, OrderSide, ProductType

    return OrderRequest(
        symbol="KPEL",
        exchange="NSE",
        side=OrderSide.SELL,
        order_type=order_type,
        quantity=10,
        product=ProductType.MIS,
        price=price,
        trigger_price=trigger,
    )


class TestPlaceOrderParams:
    """Regression #43: Kite rejects API MARKET/SL-M orders without market_protection,
    and SL (stop-limit) orders were sent with price 0."""

    def _sent(self, broker: KiteBroker, request) -> dict:
        respond(broker._kite, "place_order", kf.sdk("place_order"))  # the bare id string
        broker.place_order(request)
        return broker._kite.place_order.call_args.kwargs

    def test_market_order_sends_auto_market_protection(self, broker: KiteBroker) -> None:
        from executor.models import OrderType

        sent = self._sent(broker, _order(OrderType.MARKET))
        assert sent["order_type"] == "MARKET"
        assert sent["market_protection"] == -1

    def test_stop_market_sends_auto_market_protection(self, broker: KiteBroker) -> None:
        from executor.models import OrderType

        sent = self._sent(broker, _order(OrderType.SL, trigger=98.0))
        assert sent["order_type"] == "SL-M"
        assert sent["trigger_price"] == 98.0
        assert sent["market_protection"] == -1

    def test_limit_order_has_price_and_no_market_protection(self, broker: KiteBroker) -> None:
        from executor.models import OrderType

        sent = self._sent(broker, _order(OrderType.LIMIT, price=101.5))
        assert sent["price"] == 101.5
        assert "market_protection" not in sent

    def test_stop_limit_sends_its_limit_price(self, broker: KiteBroker) -> None:
        from executor.models import OrderType

        sent = self._sent(broker, _order(OrderType.SL_LIMIT, price=97.5, trigger=98.0))
        assert sent["order_type"] == "SL"
        assert (sent["price"], sent["trigger_price"]) == (97.5, 98.0)
        assert "market_protection" not in sent
