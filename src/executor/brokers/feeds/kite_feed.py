"""KiteFeed: the single owner of the Kite WebSocket (KiteTicker).

Design: designs/intraday-live-pipeline.md §3.1. Moved out of KiteBroker so market
data has one owner that PaperBroker (paper mode) and KiteBroker (live mode) share.
Keeps the #23 fixes: subscriptions requested before the async handshake completes
are deferred to _on_connect, and the instrument cache is per exchange.

kiteconnect notes (verified against the installed SDK):
- connect(threaded=True) starts the Twisted reactor only if it is not running, and
  close() only closes the socket, so reauthenticate() can close the old ticker and
  connect a new one with the refreshed token.
- Index ticks (NIFTY 50) carry no tradingsymbol, depth or volume: the
  token→symbol map is the only way to name them.
- ohlc["close"] is the previous session's close.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.brokers.feeds.base import (
    FeedCapabilities,
    FeedHealth,
    FeedMode,
    MarketDataFeed,
    OrderUpdateCallback,
    Tick,
    TickCallback,
)
from executor.brokers.kite_instruments import InstrumentCache

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

TickerFactory = Callable[[str, str], object]  # (api_key, access_token) -> KiteTicker


def _default_ticker_factory(api_key: str, access_token: str) -> object:
    from kiteconnect import KiteTicker  # type: ignore[import-untyped]

    return KiteTicker(api_key, access_token)


def _price_or_none(value: object) -> float | None:
    """Kite reports missing prices (empty depth level, absent field) as 0."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parse_kite_tick(raw: dict, symbol: str, exchange: str, received_at: datetime) -> Tick | None:
    """Convert one KiteTicker payload into a Tick. Returns None for unusable ticks."""
    ltp = _price_or_none(raw.get("last_price"))
    if ltp is None:
        return None
    ohlc = raw.get("ohlc") or {}
    depth = raw.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    volume = raw.get("volume_traded")
    return Tick(
        symbol=symbol,
        exchange=exchange,
        ltp=ltp,
        received_at=received_at,
        volume_traded=int(volume) if volume is not None else None,
        avg_traded_price=_price_or_none(raw.get("average_traded_price")),
        day_open=_price_or_none(ohlc.get("open")),
        day_high=_price_or_none(ohlc.get("high")),
        day_low=_price_or_none(ohlc.get("low")),
        prev_close=_price_or_none(ohlc.get("close")),
        best_bid=_price_or_none(buys[0].get("price")) if buys else None,
        best_ask=_price_or_none(sells[0].get("price")) if sells else None,
    )


class KiteFeed(MarketDataFeed):
    def __init__(
        self,
        instruments: InstrumentCache,
        mode: FeedMode = FeedMode.FULL,
        ticker_factory: TickerFactory = _default_ticker_factory,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
    ) -> None:
        self._instruments = instruments
        self._mode = mode
        self._ticker_factory = ticker_factory
        self._clock = clock
        self._ticker: object | None = None
        self._connected = False
        self._token_to_key: dict[int, tuple[str, str]] = {}  # token -> (symbol, exchange)
        self._subscribed_tokens: set[int] = set()
        self._latest: dict[str, Tick] = {}
        self._last_tick_at: datetime | None = None
        self._tick_callbacks: list[TickCallback] = []
        self._order_callbacks: list[OrderUpdateCallback] = []
        self._lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        if self._ticker is not None:
            return
        api_key = os.environ["KITE_API_KEY"]
        access_token = os.environ["KITE_ACCESS_TOKEN"]
        ticker = self._ticker_factory(api_key, access_token)
        ticker.on_ticks = self._on_ticks  # type: ignore[attr-defined]
        ticker.on_connect = self._on_connect  # type: ignore[attr-defined]
        ticker.on_close = self._on_close  # type: ignore[attr-defined]
        ticker.on_order_update = self._on_order_update  # type: ignore[attr-defined]
        self._ticker = ticker
        ticker.connect(threaded=True)  # type: ignore[attr-defined]
        logger.info(
            "kite_feed_connecting mode=%s instruments=%d", self._mode, len(self._token_to_key)
        )

    def close(self) -> None:
        ticker, self._ticker = self._ticker, None
        self._connected = False
        self._subscribed_tokens.clear()
        if ticker is not None:
            try:
                ticker.close()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("kite_feed_close_failed error=%s", exc)

    def reauthenticate(self) -> None:
        """Reconnect with the token currently in the environment; subscriptions survive
        because _token_to_key is kept and _on_connect re-subscribes all of it."""
        self.close()
        self.connect()
        logger.info("kite_feed_reauthenticated instruments=%d", len(self._token_to_key))

    # ── Subscriptions & callbacks ────────────────────────────────────────────

    def subscribe(self, symbols: list[str], exchange: str = "NSE") -> None:
        for symbol in symbols:
            try:
                token = self._instruments.token(symbol, exchange)
            except Exception as exc:
                logger.warning("kite_feed_unresolved symbol=%s:%s error=%s", exchange, symbol, exc)
                continue
            self._token_to_key[token] = (symbol, exchange)
        # Not connected yet (or handshake still in flight): _on_connect subscribes
        # everything known. Subscribing early raises inside the SDK (no ws yet).
        if self._ticker is not None and self._connected:
            new = [t for t in self._token_to_key if t not in self._subscribed_tokens]
            if new:
                self._send_subscribe(new)

    def on_tick(self, callback: TickCallback) -> None:
        self._tick_callbacks.append(callback)

    def on_order_update(self, callback: OrderUpdateCallback) -> None:
        self._order_callbacks.append(callback)

    def latest(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest.get(symbol)

    def health(self) -> FeedHealth:
        with self._lock:
            last = self._last_tick_at
        return FeedHealth(
            connected=self._connected, last_tick_at=last, subscribed=len(self._subscribed_tokens)
        )

    def capabilities(self) -> FeedCapabilities:
        return FeedCapabilities(
            has_depth=self._mode == FeedMode.FULL, has_index=True, has_historical=False
        )

    # ── KiteTicker callbacks (run on the SDK's reactor thread) ───────────────

    def _send_subscribe(self, tokens: list[int]) -> None:
        ticker = self._ticker
        try:
            ticker.subscribe(tokens)  # type: ignore[union-attr]
            sdk_mode = ticker.MODE_FULL if self._mode == FeedMode.FULL else ticker.MODE_QUOTE  # type: ignore[union-attr]
            ticker.set_mode(sdk_mode, tokens)  # type: ignore[union-attr]
            self._subscribed_tokens.update(tokens)
        except Exception as exc:
            logger.error("kite_feed_subscribe_failed tokens=%d error=%s", len(tokens), exc)

    def _on_connect(self, ws: object, response: object) -> None:
        """Fires on the first connect and on every auto-reconnect: re-subscribe everything."""
        self._connected = True
        tokens = list(self._token_to_key)
        if tokens:
            self._send_subscribe(tokens)
        logger.info("kite_feed_connected subscribed=%d", len(tokens))

    def _on_close(self, ws: object, code: object, reason: object) -> None:
        self._connected = False
        self._subscribed_tokens.clear()
        logger.warning("kite_feed_closed code=%s reason=%s", code, reason)

    def _on_ticks(self, ws: object, ticks: list[dict]) -> None:
        now = self._clock()
        parsed: list[Tick] = []
        for raw in ticks:
            key = self._token_to_key.get(raw.get("instrument_token"))  # type: ignore[arg-type]
            if key is None:
                continue
            tick = parse_kite_tick(raw, key[0], key[1], now)
            if tick is not None:
                parsed.append(tick)
        if not parsed:
            return
        with self._lock:
            for tick in parsed:
                self._latest[tick.symbol] = tick
            self._last_tick_at = now
        for tick in parsed:
            for cb in self._tick_callbacks:
                try:
                    cb(tick)
                except Exception as exc:  # one bad consumer must not starve the others
                    logger.error("kite_feed_callback_failed symbol=%s error=%s", tick.symbol, exc)

    def _on_order_update(self, ws: object, data: dict) -> None:
        for cb in self._order_callbacks:
            try:
                cb(data)
            except Exception as exc:
                logger.error("kite_feed_order_callback_failed error=%s", exc)
