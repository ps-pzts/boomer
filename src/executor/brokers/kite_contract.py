"""What Kite Connect actually returns (#84): the one source of truth for its shapes.

Each kind below is the value a `kiteconnect.KiteConnect` call hands us (or a
WebSocket order update), as documented at https://kite.trade/docs/connect/v3/.
Tests check every fake Kite response against it, and the live canary (#85)
checks real responses, so a wrong assumption about Kite fails loudly instead of
inside a live order path (the #73 `place_order` bug).

    check_shape(kind, payload) -> list[str]     # [] = matches

Required keys must be present with the right type; optional keys may be absent
but are type-checked when present; extra keys are allowed (Kite adds fields).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from kiteconnect import exceptions as kex

# ── vocabulary ────────────────────────────────────────────────────────────────

# After these an order can no longer fill.
FINAL_ORDER_STATUSES = frozenset({"COMPLETE", "CANCELLED", "CANCELLED AMO", "REJECTED"})
# Resting at the exchange: a stop awaiting its trigger, or a working order.
WORKING_ORDER_STATUSES = frozenset({"OPEN", "TRIGGER PENDING"})
# Transient while Kite / the exchange process a place, modify or cancel (#73).
IN_FLIGHT_ORDER_STATUSES = frozenset(
    {
        "PUT ORDER REQ RECEIVED",
        "VALIDATION PENDING",
        "OPEN PENDING",
        "MODIFY VALIDATION PENDING",
        "MODIFY PENDING",
        "MODIFIED",
        "CANCEL PENDING",
        "AMO REQ RECEIVED",
    }
)
ORDER_STATUSES = FINAL_ORDER_STATUSES | WORKING_ORDER_STATUSES | IN_FLIGHT_ORDER_STATUSES

ORDER_TYPES = frozenset({"MARKET", "LIMIT", "SL", "SL-M"})
TRANSACTION_TYPES = frozenset({"BUY", "SELL"})
PRODUCTS = frozenset({"CNC", "MIS", "NRML", "CO", "BO", "MTF"})
VARIETIES = frozenset({"regular", "amo", "co", "iceberg", "auction"})
VALIDITIES = frozenset({"DAY", "IOC", "TTL"})
GTT_TYPES = frozenset({"single", "two-leg"})
GTT_STATUSES = frozenset(
    {"active", "triggered", "disabled", "expired", "cancelled", "rejected", "deleted"}
)

# kiteconnect raises these, chosen by the `error_type` of Kite's error body.
EXCEPTIONS: dict[str, type[kex.KiteException]] = {
    "token": kex.TokenException,  # session expired / bad access token
    "permission": kex.PermissionException,  # e.g. LTP without the market-data plan
    "input": kex.InputException,  # bad params, trigger on the wrong side, ...
    "order": kex.OrderException,  # order placement / lookup failed
    "network": kex.NetworkException,  # Kite <-> exchange (OMS) unreachable
    "general": kex.GeneralException,  # anything unclassified
    "data": kex.DataException,  # unparseable response
}

# ── spec language ─────────────────────────────────────────────────────────────

NUM = (int, float)
STR_OR_NONE = (str, type(None))
# The SDK parses 19-char timestamps to datetime; WebSocket / stored JSON keep str.
TIMESTAMP = (dt.datetime, str)
TIMESTAMP_OR_NONE = (dt.datetime, str, type(None))


@dataclass(frozen=True)
class Opt:
    """Key may be absent; checked against `spec` when present."""

    spec: Any


@dataclass(frozen=True)
class ListOf:
    item: Any


@dataclass(frozen=True)
class OneOf:
    """A str restricted to Kite's vocabulary."""

    values: frozenset[str]
    nullable: bool = False


@dataclass(frozen=True)
class InstrumentMap:
    """{'EXCHANGE:SYMBOL': spec} as returned by ltp() / quote()."""

    item: Any


# ── shapes ────────────────────────────────────────────────────────────────────

_ORDER_COMMON: dict[str, Any] = {
    "order_id": str,
    "exchange_order_id": STR_OR_NONE,
    "parent_order_id": STR_OR_NONE,
    "placed_by": str,
    "status": OneOf(ORDER_STATUSES),
    "status_message": STR_OR_NONE,
    "status_message_raw": STR_OR_NONE,
    "variety": OneOf(VARIETIES),
    "exchange": str,
    "tradingsymbol": str,
    "instrument_token": int,
    "order_type": OneOf(ORDER_TYPES),
    "transaction_type": OneOf(TRANSACTION_TYPES),
    "validity": OneOf(VALIDITIES),
    "product": OneOf(PRODUCTS),
    "quantity": int,
    "disclosed_quantity": int,
    "price": NUM,
    "trigger_price": NUM,
    "average_price": NUM,
    "filled_quantity": int,
    "pending_quantity": int,
    "cancelled_quantity": int,
    "tag": STR_OR_NONE,
    "guid": str,
    "market_protection": Opt(NUM),
    "meta": Opt((dict, type(None))),
    "validity_ttl": Opt((int, type(None))),
    "exchange_update_timestamp": Opt(TIMESTAMP_OR_NONE),
}

ORDER = {
    **_ORDER_COMMON,
    "order_timestamp": TIMESTAMP,
    "exchange_timestamp": TIMESTAMP_OR_NONE,
    "modified": bool,
    "tags": Opt((list, type(None))),
}

ORDER_POSTBACK = {
    **_ORDER_COMMON,
    "order_timestamp": str,  # KiteTicker passes the JSON through unparsed
    "exchange_timestamp": STR_OR_NONE,
    "unfilled_quantity": Opt(int),
    "account_id": Opt(str),
    "user_id": Opt(str),
    "app_id": Opt(int),
    "checksum": Opt(str),
}

POSITION = {
    "tradingsymbol": str,
    "exchange": str,
    "instrument_token": int,
    "product": OneOf(PRODUCTS),
    "quantity": int,
    "overnight_quantity": int,
    "multiplier": NUM,
    "average_price": NUM,
    "close_price": NUM,
    "last_price": NUM,
    "value": NUM,
    "pnl": NUM,
    "m2m": NUM,
    "unrealised": NUM,
    "realised": NUM,
    "buy_quantity": int,
    "buy_price": NUM,
    "buy_value": NUM,
    "sell_quantity": int,
    "sell_price": NUM,
    "sell_value": NUM,
    "day_buy_quantity": int,
    "day_buy_price": NUM,
    "day_buy_value": NUM,
    "day_sell_quantity": int,
    "day_sell_price": NUM,
    "day_sell_value": NUM,
    "buy_m2m": Opt(NUM),
    "sell_m2m": Opt(NUM),
}

POSITIONS = {"net": ListOf(POSITION), "day": ListOf(POSITION)}

HOLDING = {
    "tradingsymbol": str,
    "exchange": str,
    "instrument_token": int,
    "isin": str,
    "product": OneOf(PRODUCTS),
    "quantity": int,
    "t1_quantity": int,
    "used_quantity": int,
    "realised_quantity": int,
    "collateral_quantity": int,
    "average_price": NUM,
    "last_price": NUM,
    "close_price": NUM,
    "pnl": NUM,
    "day_change": NUM,
    "day_change_percentage": NUM,
    "price": Opt(NUM),
    "authorised_quantity": Opt(int),
    "opening_quantity": Opt(int),
    "collateral_type": Opt(str),
    "discrepancy": Opt(bool),
}

MARGINS_EQUITY = {
    "enabled": bool,
    "net": NUM,
    "available": {
        "cash": NUM,
        "opening_balance": NUM,
        "live_balance": NUM,
        "adhoc_margin": NUM,
        "collateral": NUM,
        "intraday_payin": NUM,
    },
    "utilised": {
        "debits": NUM,
        "exposure": NUM,
        "span": NUM,
        "option_premium": NUM,
        "m2m_realised": NUM,
        "m2m_unrealised": NUM,
        "payout": NUM,
        "holding_sales": NUM,
        "turnover": NUM,
        "liquid_collateral": NUM,
        "stock_collateral": NUM,
        "delivery": Opt(NUM),
    },
}

LTP = InstrumentMap({"instrument_token": int, "last_price": NUM})

INSTRUMENT = {  # one row of instruments(), after the SDK parses the CSV dump
    "instrument_token": int,
    "exchange_token": str,
    "tradingsymbol": str,
    "name": str,
    "last_price": float,
    "expiry": (dt.date, str),  # "" for equities
    "strike": float,
    "tick_size": float,
    "lot_size": int,
    "instrument_type": str,
    "segment": str,
    "exchange": str,
}

_GTT_LEG = {
    "exchange": str,
    "tradingsymbol": str,
    "product": OneOf(PRODUCTS),
    "order_type": OneOf(ORDER_TYPES),
    "transaction_type": OneOf(TRANSACTION_TYPES),
    "quantity": int,
    "price": NUM,
    "result": (dict, type(None)),
}

GTT = {
    "id": int,
    "user_id": str,
    "parent_trigger": (int, type(None)),
    "type": OneOf(GTT_TYPES),
    "status": OneOf(GTT_STATUSES),
    "created_at": str,
    "updated_at": str,
    "expires_at": str,
    "condition": {
        "exchange": str,
        "tradingsymbol": str,
        "trigger_values": ListOf(NUM),
        "last_price": NUM,
        "instrument_token": Opt(int),
    },
    "orders": ListOf(_GTT_LEG),
    "meta": Opt((dict, type(None))),
}

GTT_ACK = {"trigger_id": int}  # place_gtt / modify_gtt / delete_gtt

SESSION = {  # generate_session()
    "user_id": str,
    "user_name": str,
    "user_shortname": str,
    "email": str,
    "user_type": str,
    "broker": str,
    "exchanges": ListOf(str),
    "products": ListOf(str),
    "order_types": ListOf(str),
    "api_key": str,
    "access_token": str,
    "public_token": str,
    "login_time": TIMESTAMP,
    "refresh_token": Opt(str),
    "enctoken": Opt(str),
    "avatar_url": Opt(STR_OR_NONE),
    "meta": Opt((dict, type(None))),
}

CANDLE = {  # one row of historical_data()
    "date": dt.datetime,
    "open": NUM,
    "high": NUM,
    "low": NUM,
    "close": NUM,
    "volume": int,
    "oi": Opt(int),
}

KINDS: dict[str, Any] = {
    "order": ORDER,
    "orders": ListOf(ORDER),
    "order_postback": ORDER_POSTBACK,
    "position": POSITION,
    "positions": POSITIONS,
    "holding": HOLDING,
    "holdings": ListOf(HOLDING),
    "margins_equity": MARGINS_EQUITY,
    "ltp": LTP,
    "instrument": INSTRUMENT,
    "instruments": ListOf(INSTRUMENT),
    "gtt": GTT,
    "gtts": ListOf(GTT),
    "gtt_ack": GTT_ACK,
    "place_order": str,  # the order id itself; also modify_order / cancel_order
    "session": SESSION,
    "historical": ListOf(CANDLE),
}


# ── checker ───────────────────────────────────────────────────────────────────


def check_shape(kind: str, payload: Any) -> list[str]:
    """Problems with `payload` as a Kite `kind` response; [] means it matches.

    Raises KeyError for an unknown kind (a typo must not pass silently)."""
    if kind not in KINDS:
        raise KeyError(f"unknown Kite contract kind {kind!r}; known: {sorted(KINDS)}")
    problems: list[str] = []
    _check(KINDS[kind], payload, kind, problems)
    return problems


def _type_names(types: tuple[type, ...]) -> str:
    return " | ".join("None" if t is type(None) else t.__name__ for t in types)


def _check(spec: Any, value: Any, path: str, out: list[str]) -> None:
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            out.append(f"{path}: expected object, got {type(value).__name__}")
            return
        for key, sub in spec.items():
            optional = isinstance(sub, Opt)
            if key not in value:
                if not optional:
                    out.append(f"{path}.{key}: missing")
                continue
            _check(sub.spec if optional else sub, value[key], f"{path}.{key}", out)
    elif isinstance(spec, ListOf):
        if not isinstance(value, list):
            out.append(f"{path}: expected list, got {type(value).__name__}")
            return
        for i, item in enumerate(value):
            _check(spec.item, item, f"{path}[{i}]", out)
    elif isinstance(spec, InstrumentMap):
        if not isinstance(value, dict):
            out.append(f"{path}: expected object, got {type(value).__name__}")
            return
        for key, item in value.items():
            exchange, _, symbol = str(key).partition(":")
            if not (exchange and symbol):
                out.append(f"{path}: key {key!r} is not EXCHANGE:SYMBOL")
                continue
            _check(spec.item, item, f"{path}[{key!r}]", out)
    elif isinstance(spec, OneOf):
        if value is None and spec.nullable:
            return
        if not isinstance(value, str):
            out.append(f"{path}: expected str, got {type(value).__name__}")
        elif value not in spec.values:
            out.append(f"{path}: {value!r} not in Kite's vocabulary")
    else:
        types = spec if isinstance(spec, tuple) else (spec,)
        # bool is an int subclass; Kite never sends true/false for a number.
        wrong_bool = isinstance(value, bool) and bool not in types
        if wrong_bool or not isinstance(value, types):
            out.append(f"{path}: expected {_type_names(types)}, got {type(value).__name__}")
