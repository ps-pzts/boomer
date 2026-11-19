"""A stateful Kite SDK fake for tests (#85).

Built on `create_autospec(kiteconnect.KiteConnect, instance=True)`, so every call
is checked against the real SDK's signatures: a renamed or misspelt argument
fails the test the way it would fail against Kite. The side effects keep a tiny
order book so place / modify / cancel / orders() agree with each other.
"""

from __future__ import annotations

import itertools
from unittest.mock import create_autospec

import kiteconnect

from executor.brokers.kite_broker import KiteBroker

INFY_ROW = {
    "instrument_token": 408065,
    "exchange_token": "1594",
    "tradingsymbol": "INFY",
    "name": "INFOSYS",
    "last_price": 0.0,
    "expiry": "",
    "strike": 0.0,
    "tick_size": 0.1,
    "lot_size": 1,
    "instrument_type": "EQ",
    "segment": "NSE",
    "exchange": "NSE",
}


class FakeKite:
    """Wraps an autospecced KiteConnect; `sdk` is what KiteBroker talks to."""

    def __init__(self, ltp: float = 1500.0) -> None:
        self.sdk = create_autospec(kiteconnect.KiteConnect, instance=True)
        self.sdk.VARIETY_REGULAR = "regular"
        self.ltp = ltp
        self.book: dict[str, dict] = {}
        self._ids = itertools.count(250001)
        # Behaviour switches for failure tests
        self.reject_place: set[str] = set()  # transaction types placed as REJECTED
        self.fill_on_place: set[str] = set()  # transaction types that fill at once
        self.cancel_raises = False
        self.place_raises: Exception | None = None
        self.ltp_script: list[float] = []  # successive LTPs, last one sticks
        s = self.sdk
        s.profile.side_effect = lambda: {"user_id": "AB1234", "user_name": "Test", "email": "x"}
        s.margins.side_effect = self._margins
        s.orders.side_effect = lambda: [dict(o) for o in self.book.values()]
        s.positions.side_effect = lambda: {"net": [], "day": []}
        s.holdings.side_effect = lambda: []
        s.instruments.side_effect = lambda exchange=None: [dict(INFY_ROW)]
        s.ltp.side_effect = self._ltp
        s.place_order.side_effect = self._place
        s.modify_order.side_effect = self._modify
        s.cancel_order.side_effect = self._cancel

    def broker(self) -> KiteBroker:
        b = KiteBroker()
        b._kite = self.sdk
        return b

    def _margins(self, segment=None):
        return {
            "enabled": True,
            "net": 100000.0,
            "available": {"cash": 100000.0, "live_balance": 100000.0, "opening_balance": 1e5},
            "utilised": {"debits": 0.0, "exposure": 0.0, "m2m_realised": 0.0},
        }

    def _ltp(self, *instruments):
        if len(self.ltp_script) > 1:
            self.ltp = self.ltp_script.pop(0)
        elif self.ltp_script:
            self.ltp = self.ltp_script[0]
        keys = instruments[0] if len(instruments) == 1 and isinstance(instruments[0], list) else []
        keys = keys or list(instruments)
        return {k: {"instrument_token": 408065, "last_price": self.ltp} for k in keys}

    def _place(self, variety, exchange, tradingsymbol, transaction_type, quantity, product,
               order_type, price=None, validity=None, validity_ttl=None,
               disclosed_quantity=None, trigger_price=None, iceberg_legs=None,
               iceberg_quantity=None, auction_number=None, algo_id=None, tag=None,
               market_protection=None):  # fmt: skip
        if self.place_raises is not None:
            raise self.place_raises
        oid = str(next(self._ids))
        status = "TRIGGER PENDING" if order_type in ("SL", "SL-M") else "OPEN"
        filled = 0
        if transaction_type in self.reject_place:
            status = "REJECTED"
        elif transaction_type in self.fill_on_place:
            status, filled = "COMPLETE", quantity
        self.book[oid] = {
            "order_id": oid,
            "exchange_order_id": None,
            "status": status,
            "status_message": "RMS: rejected" if status == "REJECTED" else None,
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": transaction_type,
            "order_type": order_type,
            "product": product,
            "variety": variety,
            "validity": validity,
            "quantity": quantity,
            "filled_quantity": filled,
            "pending_quantity": quantity - filled,
            "price": price or 0.0,
            "trigger_price": trigger_price or 0.0,
            "average_price": self.ltp if filled else 0.0,
            "tag": tag,
            "order_timestamp": "2026-11-19 09:15:05",
        }
        return oid

    def _modify(self, variety, order_id, parent_order_id=None, quantity=None, price=None,
                order_type=None, trigger_price=None, validity=None, disclosed_quantity=None,
                market_protection=None):  # fmt: skip
        o = self.book[order_id]
        if price is not None:
            o["price"] = price
        if trigger_price is not None:
            o["trigger_price"] = trigger_price
        return order_id

    def _cancel(self, variety, order_id, parent_order_id=None):
        if self.cancel_raises:
            raise kiteconnect.exceptions.InputException("Order cannot be cancelled")
        o = self.book[order_id]
        if o["status"] in ("OPEN", "TRIGGER PENDING"):
            o["status"] = "CANCELLED"
        return order_id
