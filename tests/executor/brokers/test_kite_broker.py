"""Tests for KiteBroker.get_ltp() fallback chain and place_gtt() LTP guard.

Regression coverage for the silent bad-fallback bug:
  place_gtt() used `get_ltp() or sl_trigger_price` which caused Kite to reject
  the order with "Trigger cannot be created with one of the trigger values equal
  to the last price." when get_ltp() returned None.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.executor.brokers.kite_broker import KiteBroker
from src.executor.models import GttRequest, GttType

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def broker() -> KiteBroker:
    b = KiteBroker()
    b._kite = MagicMock()
    b._kite.GTT_TYPE_SINGLE = "single"
    b._kite.GTT_TYPE_OCO = "two-leg"
    return b


# ── get_ltp() fallback chain ──────────────────────────────────────────────────


class TestGetLtp:
    def test_tier1_rest_ltp_used_when_available(self, broker: KiteBroker) -> None:
        broker._kite.ltp.return_value = {"NSE:KPEL": {"last_price": 380.5}}
        assert broker.get_ltp("KPEL", "NSE") == 380.5

    def test_tier2_holdings_fallback_when_rest_fails(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("Insufficient permission")
        broker._kite.holdings.return_value = [
            {"tradingsymbol": "KPEL", "last_price": 375.0},
        ]
        broker._kite.positions.return_value = {"day": []}
        assert broker.get_ltp("KPEL", "NSE") == 375.0

    def test_tier2_positions_fallback_when_rest_and_holdings_miss(
        self, broker: KiteBroker
    ) -> None:
        broker._kite.ltp.side_effect = Exception("Insufficient permission")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {
            "day": [{"tradingsymbol": "KPEL", "last_price": 362.0}]
        }
        assert broker.get_ltp("KPEL", "NSE") == 362.0

    def test_tier3_stale_tick_cache_used_as_last_resort(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no data")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}
        broker._ltp["KPEL"] = 355.0  # stale but present
        assert broker.get_ltp("KPEL", "NSE") == 355.0

    def test_returns_none_when_all_tiers_exhausted(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no data")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}
        assert broker.get_ltp("KPEL", "NSE") is None

    def test_zero_last_price_in_holdings_is_skipped(self, broker: KiteBroker) -> None:
        """Holdings entries with last_price=0 (delisted / not yet priced) must be skipped."""
        broker._kite.ltp.side_effect = Exception("no data")
        broker._kite.holdings.return_value = [
            {"tradingsymbol": "KPEL", "last_price": 0},
        ]
        broker._kite.positions.return_value = {"day": []}
        assert broker.get_ltp("KPEL", "NSE") is None


# ── place_gtt() LTP guard ─────────────────────────────────────────────────────


class TestPlaceGttLtpGuard:
    def _oco_request(self) -> GttRequest:
        return GttRequest(
            symbol="KPEL", exchange="NSE",
            gtt_type=GttType.OCO, quantity=18,
            sl_trigger_price=338.03, sl_limit_price=337.36,
            target_trigger_price=439.46, target_limit_price=438.58,
        )

    def test_raises_when_ltp_unavailable(self, broker: KiteBroker) -> None:
        """place_gtt must raise RuntimeError — not silently use sl_trigger as last_price."""
        broker._kite.ltp.side_effect = Exception("Insufficient permission")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}

        with pytest.raises(RuntimeError, match="LTP unavailable"):
            broker.place_gtt(self._oco_request())

        broker._kite.place_gtt.assert_not_called()

    def test_oco_uses_ltp_from_rest(self, broker: KiteBroker) -> None:
        """place_gtt passes the real LTP — not the sl_trigger — to the Kite SDK."""
        broker._kite.ltp.return_value = {"NSE:KPEL": {"last_price": 360.0}}
        broker._kite.place_gtt.return_value = {"trigger_id": 12345}

        broker.place_gtt(self._oco_request())

        call_kwargs = broker._kite.place_gtt.call_args[1]
        assert call_kwargs["last_price"] == 360.0
        # Confirm the bad-fallback value (sl_trigger) was NOT used
        assert call_kwargs["last_price"] != 338.03

    def test_oco_uses_ltp_from_holdings_fallback(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no market data")
        broker._kite.holdings.return_value = [
            {"tradingsymbol": "KPEL", "last_price": 360.0}
        ]
        broker._kite.positions.return_value = {"day": []}
        broker._kite.place_gtt.return_value = {"trigger_id": 12345}

        broker.place_gtt(self._oco_request())

        assert broker._kite.place_gtt.call_args[1]["last_price"] == 360.0

    def test_single_gtt_raises_when_ltp_unavailable(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no market data")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}

        req = GttRequest(
            symbol="KPEL", exchange="NSE",
            gtt_type=GttType.SINGLE, quantity=18,
            trigger_price=338.03, limit_price=337.36,
        )
        with pytest.raises(RuntimeError, match="LTP unavailable"):
            broker.place_gtt(req)


# ── Ticker subscription (KiteTicker wiring) ────────────────────────────────────


class _FakeTicker:
    """Stand-in for kiteconnect.KiteTicker — records subscribe/mode calls."""

    MODE_QUOTE = "quote"

    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.subscribe_calls: list[list[int]] = []
        self.mode_calls: list[tuple[str, list[int]]] = []
        self.on_ticks = None
        self.on_order_update = None
        self.on_connect = None
        self.on_close = None

    def connect(self, threaded: bool = False) -> None:
        pass  # real SDK connects asynchronously; tests fire on_connect manually

    def subscribe(self, tokens: list[int]) -> None:
        self.subscribe_calls.append(list(tokens))

    def set_mode(self, mode: str, tokens: list[int]) -> None:
        self.mode_calls.append((mode, list(tokens)))


class TestTickerSubscription:
    """Regression coverage for the silent no-subscribe bug: KiteTicker connected
    but never called subscribe()/set_mode(), so zero ticks ever arrived."""

    def _mock_one_instrument(self, broker: KiteBroker, symbol: str, token: int) -> None:
        broker._kite.instruments.return_value = [
            {"tradingsymbol": symbol, "instrument_token": token},
        ]

    def test_on_connect_subscribes_all_known_tokens(self, broker: KiteBroker) -> None:
        """Once the WS handshake completes, all resolved tokens must be subscribed."""
        self._mock_one_instrument(broker, "KPEL", 12345)
        fake_ticker = _FakeTicker("key", "token")
        broker._ticker = fake_ticker

        broker._start_ticker_if_needed(["KPEL"])
        # Not connected yet — must NOT subscribe prematurely (the bug this guards).
        assert fake_ticker.subscribe_calls == []

        broker._on_ticker_connect(ws=object(), response={})

        assert fake_ticker.subscribe_calls == [[12345]]
        assert fake_ticker.mode_calls == [(_FakeTicker.MODE_QUOTE, [12345])]

    def test_new_tokens_subscribed_immediately_once_connected(self, broker: KiteBroker) -> None:
        """A later on_tick() call, once already connected, subscribes right away."""
        self._mock_one_instrument(broker, "KPEL", 12345)
        fake_ticker = _FakeTicker("key", "token")
        broker._ticker = fake_ticker
        broker._ticker_connected = True

        broker._start_ticker_if_needed(["KPEL"])

        assert fake_ticker.subscribe_calls == [[12345]]

    def test_new_tokens_deferred_when_handshake_not_yet_complete(
        self, broker: KiteBroker
    ) -> None:
        """Regression: subscribe() must never be called before the WS is connected —
        calling it early raises inside the real kiteconnect SDK (no self._ticker.ws)."""
        self._mock_one_instrument(broker, "KPEL", 12345)
        fake_ticker = _FakeTicker("key", "token")
        broker._ticker = fake_ticker
        broker._ticker_connected = False  # handshake still in flight

        broker._start_ticker_if_needed(["KPEL"])  # must not raise, must not subscribe

        assert fake_ticker.subscribe_calls == []
        assert 12345 in broker._token_to_symbol

    def test_on_ticker_close_resets_connected_state(self, broker: KiteBroker) -> None:
        fake_ticker = _FakeTicker("key", "token")
        broker._ticker = fake_ticker
        broker._ticker_connected = True
        broker._subscribed_tokens = {12345}

        broker._on_ticker_close(ws=object(), code=1006, reason="abnormal closure")

        assert broker._ticker_connected is False
        assert broker._subscribed_tokens == set()


# ── _on_ticks_received() → symbol resolution and LTP updates ──────────────────


class TestOnTicksReceived:
    def test_maps_instrument_token_to_symbol(self, broker: KiteBroker) -> None:
        broker._token_to_symbol = {12345: "KPEL"}
        received: list[tuple[str, float]] = []
        broker._tick_subscriptions = [(["KPEL"], lambda sym, ltp: received.append((sym, ltp)))]

        broker._on_ticks_received(
            ws=object(), ticks=[{"instrument_token": 12345, "last_price": 401.5}]
        )

        assert received == [("KPEL", 401.5)]
        assert broker._ltp["KPEL"] == 401.5

    def test_ignores_tick_with_zero_last_price(self, broker: KiteBroker) -> None:
        broker._token_to_symbol = {12345: "KPEL"}
        received: list[tuple[str, float]] = []
        broker._tick_subscriptions = [(["KPEL"], lambda sym, ltp: received.append((sym, ltp)))]

        broker._on_ticks_received(
            ws=object(), ticks=[{"instrument_token": 12345, "last_price": 0}]
        )

        assert received == []
        assert "KPEL" not in broker._ltp

    def test_unknown_token_without_tradingsymbol_is_dropped(self, broker: KiteBroker) -> None:
        """A tick for a token we never subscribed (and with no tradingsymbol field,
        which is the real-world case) must be dropped, not raise."""
        broker._token_to_symbol = {}
        broker._tick_subscriptions = []

        broker._on_ticks_received(
            ws=object(), ticks=[{"instrument_token": 99999, "last_price": 100.0}]
        )

        assert broker._ltp == {}


# ── _resolve_instrument_token() caching ────────────────────────────────────────


class TestResolveInstrumentToken:
    def test_caches_per_exchange_not_globally(self, broker: KiteBroker) -> None:
        """Regression: a single global cache would silently misresolve or fail
        lookups for a second exchange once the first exchange's list was cached."""
        broker._kite.instruments.side_effect = [
            [{"tradingsymbol": "KPEL", "instrument_token": 111}],  # NSE
            [{"tradingsymbol": "KPEL", "instrument_token": 222}],  # BSE
        ]

        nse_token = broker._resolve_instrument_token("KPEL", "NSE")
        bse_token = broker._resolve_instrument_token("KPEL", "BSE")

        assert nse_token == 111
        assert bse_token == 222
        assert broker._kite.instruments.call_count == 2

    def test_second_lookup_same_exchange_uses_cache(self, broker: KiteBroker) -> None:
        broker._kite.instruments.return_value = [
            {"tradingsymbol": "KPEL", "instrument_token": 111},
        ]

        broker._resolve_instrument_token("KPEL", "NSE")
        broker._resolve_instrument_token("KPEL", "NSE")

        assert broker._kite.instruments.call_count == 1
