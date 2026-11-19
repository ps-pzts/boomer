"""Kite Connect responses for tests (#84). See tests/fixtures/kite/README.md.

`load` / `data` give the documented wire payloads; `sdk` runs the real
kiteconnect method over them (fake HTTP session, never the network) so tests
see exactly the Python values the SDK hands KiteBroker.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import requests
from kiteconnect import KiteConnect

FIXTURES = Path(__file__).parent / "fixtures" / "kite"

# SDK method -> (fixture file, positional args, keyword args) of a typical call.
_CALLS: dict[str, tuple[str, tuple, dict]] = {
    "orders": ("orders.json", (), {}),
    "positions": ("positions.json", (), {}),
    "holdings": ("holdings.json", (), {}),
    "margins": ("margins_equity.json", (), {"segment": "equity"}),
    "ltp": ("ltp.json", ("NSE:INFY",), {}),
    "instruments": ("instruments_nse.csv", (), {"exchange": "NSE"}),
    "historical_data": (
        "historical_day.json",
        (408065, "2024-06-07", "2024-06-10", "day"),
        {},
    ),
    "get_gtt": ("gtt.json", (123456,), {}),
    "get_gtts": ("gtts.json", (), {}),
    "place_gtt": (
        "place_gtt.json",
        (),
        {
            "trigger_type": "two-leg",
            "tradingsymbol": "KPEL",
            "exchange": "NSE",
            "trigger_values": [338.05, 439.45],
            "last_price": 360.0,
            "orders": [
                {
                    "transaction_type": "SELL",
                    "quantity": 18,
                    "product": "CNC",
                    "order_type": "LIMIT",
                    "price": p,
                }
                for p in (337.35, 438.6)
            ],
        },
    ),
    "place_order": (
        "place_order.json",
        (),
        {
            "variety": "regular",
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "transaction_type": "BUY",
            "quantity": 10,
            "product": "MIS",
            "order_type": "LIMIT",
            "price": 1502.5,
        },
    ),
    "generate_session": ("session.json", ("request_token", "api_secret"), {}),
}


@cache
def _read(name: str) -> Any:
    path = FIXTURES / name
    if path.suffix == ".csv":
        return path.read_bytes()
    return json.loads(path.read_text())


def load(name: str) -> Any:
    """The fixture file as Kite sends it (a fresh copy; bytes for CSV)."""
    return copy.deepcopy(_read(name))


def data(name: str) -> Any:
    """The payload inside the envelope: `data` of an HTTP body or WebSocket message."""
    return load(name)["data"]


def postback() -> dict:
    """One WebSocket order update, as KiteTicker passes it to on_order_update."""
    return data("order_postback.json")


def _response(status: int, body: Any, content_type: str) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.headers["content-type"] = content_type
    r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return r


def _offline_kite(response: requests.Response) -> KiteConnect:
    kite = KiteConnect(api_key="fixture", access_token="fixture")
    kite.reqsession = MagicMock()
    kite.reqsession.request.return_value = response
    return kite


def sdk(method: str, fixture: str | None = None, transform=None) -> Any:
    """What `KiteConnect.<method>()` returns when Kite answers with `fixture`.

    `transform(payload)` may edit the wire payload first (e.g. pick one order's
    status) so a variant still goes through the SDK's own parsing."""
    default, args, kwargs = _CALLS[method]
    name = fixture or default
    body = load(name)
    if transform is not None:
        if isinstance(body, dict) and "data" in body:
            body["data"] = transform(body["data"])
        else:
            body = transform(body)
    ctype = "text/csv" if name.endswith(".csv") else "application/json"
    return getattr(_offline_kite(_response(200, body, ctype)), method)(*args, **kwargs)


def sdk_error(kind: str) -> Exception:
    """The kiteconnect exception the SDK raises for a documented error body
    (`errors.json` key: token, permission, input, order, network, general, data)."""
    err = load("errors.json")[kind]
    kite = _offline_kite(_response(err["http_status"], err["body"], "application/json"))
    try:
        kite.orders()
    except Exception as exc:  # the SDK's own exception class for this error_type
        return exc
    raise AssertionError(f"errors.json[{kind!r}] did not raise")


def orders_where(**match: Any) -> list[dict]:
    """SDK-parsed orders from orders.json whose fields equal `match`."""
    return [o for o in sdk("orders") if all(o[k] == v for k, v in match.items())]
