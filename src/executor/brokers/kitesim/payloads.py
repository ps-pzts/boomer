"""Kite-shaped payloads built from KiteSim's internal order records.

An internal order is a Kite order dict (string timestamps, as on the wire) plus
private `_`-prefixed fields. `order_view` is what `orders()` returns after the
SDK's parsing (timestamps -> datetime); `postback` is what KiteTicker hands
`on_order_update` (strings kept). Both must pass kite_contract.check_shape.
"""

from __future__ import annotations

from datetime import datetime

_TS_FMT = "%Y-%m-%d %H:%M:%S"
# kiteconnect parses these when they are 19-char timestamps (KiteConnect._format_response).
_SDK_PARSED = ("order_timestamp", "exchange_timestamp")


def ts(now: datetime) -> str:
    return now.strftime(_TS_FMT)


def new_order(
    *,
    order_id: str,
    guid: str,
    user: str,
    variety: str,
    exchange: str,
    symbol: str,
    token: int,
    side: str,
    order_type: str,
    product: str,
    quantity: int,
    price: float,
    trigger_price: float,
    validity: str,
    tag: str | None,
    market_protection: float | None,
    now: str,
    seq: int,
) -> dict:
    return {
        "placed_by": user,
        "order_id": order_id,
        "exchange_order_id": None,
        "parent_order_id": None,
        "status": "PUT ORDER REQ RECEIVED",
        "status_message": None,
        "status_message_raw": None,
        "order_timestamp": now,
        "exchange_update_timestamp": None,
        "exchange_timestamp": None,
        "variety": variety,
        "modified": False,
        "exchange": exchange,
        "tradingsymbol": symbol,
        "instrument_token": token,
        "order_type": order_type,
        "transaction_type": side,
        "validity": validity,
        "validity_ttl": 0,
        "product": product,
        "quantity": quantity,
        "disclosed_quantity": 0,
        "price": float(price or 0.0),
        "trigger_price": float(trigger_price or 0.0),
        "average_price": 0.0,
        "filled_quantity": 0,
        "pending_quantity": quantity,
        "cancelled_quantity": 0,
        "market_protection": float(market_protection or 0),
        "meta": {},
        "tag": tag,
        "tags": [tag] if tag else [],
        "guid": guid,
        "_seq": seq,
        "_blocked": 0.0,
    }


def _public(order: dict) -> dict:
    return {k: v for k, v in order.items() if not k.startswith("_")}


def order_view(order: dict) -> dict:
    """One row of `orders()` / `order_history()`, as the SDK returns it."""
    out = _public(order)
    for field in _SDK_PARSED:
        value = out.get(field)
        if isinstance(value, str) and len(value) == 19:
            out[field] = datetime.strptime(value, _TS_FMT)
    out["tags"] = list(out.get("tags") or [])
    out["meta"] = dict(out.get("meta") or {})
    return out


def postback(order: dict, user: str) -> dict:
    """The WebSocket order update KiteTicker passes to on_order_update."""
    out = _public(order)
    out.pop("modified", None)
    out.pop("tags", None)
    out["meta"] = dict(out.get("meta") or {})
    out.update(
        {
            "account_id": user,
            "unfilled_quantity": int(order["pending_quantity"]),
            "checksum": "",
            "app_id": 0,
        }
    )
    return out


def profile(user: str) -> dict:
    return {
        "user_id": user,
        "user_type": "individual",
        "email": f"{user.lower()}@paper.invalid",
        "user_name": "Paper Trader",
        "user_shortname": "Paper",
        "broker": "ZERODHA",
        "exchanges": ["NSE", "BSE"],
        "products": ["CNC", "NRML", "MIS"],
        "order_types": ["MARKET", "LIMIT", "SL", "SL-M"],
        "avatar_url": None,
        "meta": {"demat_consent": "physical"},
    }
