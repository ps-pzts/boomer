"""Tests for KiteFeed — the single owner of the Kite WebSocket.

Ported from test_kite_broker.py when the ticker moved out of KiteBroker (B7 PR 1).
Keeps the regression coverage for the #22/#23 bugs: connected-but-never-subscribed,
and subscribe() called before the async handshake completed.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from executor.brokers.feeds.base import FeedMode, Tick
from executor.brokers.feeds.kite_feed import KiteFeed, parse_kite_tick
from executor.brokers.kite_instruments import InstrumentCache

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 19, 10, 0, 0, tzinfo=IST)


class _FakeTicker:
    """Stand-in for kiteconnect.KiteTicker — records calls, never touches the network."""

    MODE_QUOTE = "quote"
    MODE_FULL = "full"
    instances: list[_FakeTicker] = []

    def __init__(self, api_key: str, access_token: str) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.subscribe_calls: list[list[int]] = []
        self.mode_calls: list[tuple[str, list[int]]] = []
        self.connected_threaded: bool | None = None
        self.closed = False
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_order_update = None
        _FakeTicker.instances.append(self)

    def connect(self, threaded: bool = False) -> None:
        self.connected_threaded = threaded  # real SDK connects asynchronously

    def close(self) -> None:
        self.closed = True

    def subscribe(self, tokens: list[int]) -> None:
        self.subscribe_calls.append(list(tokens))

    def set_mode(self, mode: str, tokens: list[int]) -> None:
        self.mode_calls.append((mode, list(tokens)))


_INSTRUMENTS = {
    "NSE": [
        {"tradingsymbol": "KPEL", "instrument_token": 12345},
        {"tradingsymbol": "INFY", "instrument_token": 408065},
        {"tradingsymbol": "NIFTY 50", "instrument_token": 256265},
    ]
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "token-day-1")
    _FakeTicker.instances.clear()


def _feed(mode: FeedMode = FeedMode.FULL) -> KiteFeed:
    return KiteFeed(
        instruments=InstrumentCache(lambda ex: _INSTRUMENTS.get(ex, [])),
        mode=mode,
        ticker_factory=_FakeTicker,
        clock=lambda: NOW,
    )


def _ticker(feed: KiteFeed) -> _FakeTicker:
    return feed._ticker  # type: ignore[return-value]


# ── Subscription timing (#22 / #23 regressions) ───────────────────────────────


class TestSubscription:
    def test_subscribe_before_connect_is_deferred_until_handshake(self) -> None:
        feed = _feed()
        feed.subscribe(["KPEL"])
        feed.connect()
        assert _ticker(feed).subscribe_calls == []  # handshake still in flight

        feed._on_connect(ws=object(), response={})

        assert _ticker(feed).subscribe_calls == [[12345]]
        assert _ticker(feed).mode_calls == [("full", [12345])]

    def test_subscribe_while_connecting_does_not_call_sdk(self) -> None:
        """Regression (#23): subscribe() before the WS exists raises inside the SDK."""
        feed = _feed()
        feed.connect()
        feed.subscribe(["KPEL"])  # must not raise, must not subscribe yet
        assert _ticker(feed).subscribe_calls == []
        feed._on_connect(ws=object(), response={})
        assert _ticker(feed).subscribe_calls == [[12345]]

    def test_new_symbols_subscribed_immediately_once_connected(self) -> None:
        feed = _feed()
        feed.connect()
        feed._on_connect(ws=object(), response={})
        feed.subscribe(["INFY"])
        assert _ticker(feed).subscribe_calls == [[408065]]

    def test_already_subscribed_symbols_not_resent(self) -> None:
        feed = _feed()
        feed.subscribe(["KPEL"])
        feed.connect()
        feed._on_connect(ws=object(), response={})
        feed.subscribe(["KPEL"])
        assert _ticker(feed).subscribe_calls == [[12345]]

    def test_quote_mode_uses_sdk_quote_constant(self) -> None:
        feed = _feed(FeedMode.QUOTE)
        feed.subscribe(["KPEL"])
        feed.connect()
        feed._on_connect(ws=object(), response={})
        assert _ticker(feed).mode_calls == [("quote", [12345])]

    def test_unknown_symbol_is_skipped_not_raised(self) -> None:
        feed = _feed()
        feed.subscribe(["NOPE", "KPEL"])
        feed.connect()
        feed._on_connect(ws=object(), response={})
        assert _ticker(feed).subscribe_calls == [[12345]]

    def test_on_close_resets_connected_state_and_reconnect_resubscribes(self) -> None:
        feed = _feed()
        feed.subscribe(["KPEL"])
        feed.connect()
        feed._on_connect(ws=object(), response={})
        feed._on_close(ws=object(), code=1006, reason="abnormal closure")
        assert feed.health().connected is False
        assert feed.health().subscribed == 0

        feed._on_connect(ws=object(), response={})  # SDK auto-reconnect
        assert _ticker(feed).subscribe_calls == [[12345], [12345]]

    def test_connect_is_threaded_and_idempotent(self) -> None:
        feed = _feed()
        feed.connect()
        feed.connect()
        assert len(_FakeTicker.instances) == 1
        assert _ticker(feed).connected_threaded is True


# ── Reauthentication (design loophole L6) ─────────────────────────────────────


class TestReauthenticate:
    def test_reconnects_with_fresh_token_and_keeps_subscriptions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feed = _feed()
        feed.subscribe(["KPEL", "NIFTY 50"])
        feed.connect()
        feed._on_connect(ws=object(), response={})
        old = _ticker(feed)

        monkeypatch.setenv("KITE_ACCESS_TOKEN", "token-day-2")
        feed.reauthenticate()
        new = _ticker(feed)

        assert old.closed is True
        assert new is not old
        assert new.access_token == "token-day-2"
        new.on_connect(object(), {})  # type: ignore[misc]
        assert sorted(new.subscribe_calls[0]) == [12345, 256265]


# ── Tick handling ─────────────────────────────────────────────────────────────


def _full_equity_tick(token: int = 12345, ltp: float = 401.5) -> dict:
    return {
        "instrument_token": token,
        "last_price": ltp,
        "average_traded_price": 399.8,
        "volume_traded": 120_000,
        "ohlc": {"open": 395.0, "high": 403.0, "low": 394.0, "close": 390.0},
        "depth": {
            "buy": [{"price": 401.45, "quantity": 10, "orders": 2}],
            "sell": [{"price": 401.55, "quantity": 8, "orders": 1}],
        },
    }


class TestTicks:
    def test_full_tick_parsed_into_tick(self) -> None:
        feed = _feed()
        feed.subscribe(["KPEL"])
        received: list[Tick] = []
        feed.on_tick(received.append)

        feed._on_ticks(ws=object(), ticks=[_full_equity_tick()])

        assert received == [
            Tick(
                symbol="KPEL",
                exchange="NSE",
                ltp=401.5,
                received_at=NOW,
                volume_traded=120_000,
                avg_traded_price=399.8,
                day_open=395.0,
                day_high=403.0,
                day_low=394.0,
                prev_close=390.0,
                best_bid=401.45,
                best_ask=401.55,
            )
        ]
        assert feed.latest("KPEL") == received[0]
        assert feed.health().last_tick_at == NOW

    def test_index_tick_named_via_token_map(self) -> None:
        """Index ticks carry no tradingsymbol, depth or volume."""
        feed = _feed()
        feed.subscribe(["NIFTY 50"])
        raw = {
            "instrument_token": 256265,
            "last_price": 24_310.5,
            "ohlc": {"open": 24_250.0, "high": 24_330.0, "low": 24_200.0, "close": 24_280.0},
        }
        feed._on_ticks(ws=object(), ticks=[raw])
        tick = feed.latest("NIFTY 50")
        assert tick is not None
        assert tick.prev_close == 24_280.0
        assert tick.best_bid is None and tick.volume_traded is None

    def test_zero_last_price_dropped(self) -> None:
        feed = _feed()
        feed.subscribe(["KPEL"])
        feed._on_ticks(ws=object(), ticks=[_full_equity_tick(ltp=0)])
        assert feed.latest("KPEL") is None
        assert feed.health().last_tick_at is None

    def test_unknown_token_dropped(self) -> None:
        feed = _feed()
        feed._on_ticks(ws=object(), ticks=[_full_equity_tick(token=99999)])
        assert feed.latest("KPEL") is None

    def test_failing_callback_does_not_starve_others(self) -> None:
        feed = _feed()
        feed.subscribe(["KPEL"])
        got: list[str] = []

        def boom(t: Tick) -> None:
            raise RuntimeError("consumer bug")

        feed.on_tick(boom)
        feed.on_tick(lambda t: got.append(t.symbol))
        feed._on_ticks(ws=object(), ticks=[_full_equity_tick()])
        assert got == ["KPEL"]

    def test_order_updates_relayed(self) -> None:
        feed = _feed()
        got: list[dict] = []
        feed.on_order_update(got.append)
        feed._on_order_update(ws=object(), data={"order_id": "1"})
        assert got == [{"order_id": "1"}]


class TestParseKiteTick:
    def test_empty_depth_level_reported_as_zero_becomes_none(self) -> None:
        raw = _full_equity_tick()
        raw["depth"] = {"buy": [{"price": 0, "quantity": 0}], "sell": []}
        tick = parse_kite_tick(raw, "KPEL", "NSE", NOW)
        assert tick is not None
        assert tick.best_bid is None and tick.best_ask is None

    def test_missing_ltp_returns_none(self) -> None:
        assert parse_kite_tick({"instrument_token": 1}, "KPEL", "NSE", NOW) is None


class TestCapabilities:
    def test_full_mode_has_depth_and_no_historical(self) -> None:
        caps = _feed(FeedMode.FULL).capabilities()
        assert caps.has_depth is True
        assert caps.has_historical is False  # Kite Personal: design §1b

    def test_quote_mode_has_no_depth(self) -> None:
        assert _feed(FeedMode.QUOTE).capabilities().has_depth is False
