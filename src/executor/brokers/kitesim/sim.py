"""KiteSim's kiteconnect-shaped API (#86).

Every public method below has exactly `kiteconnect.KiteConnect`'s signature (a
test compares them), takes Kite's own strings and returns what the SDK returns.
Each body is one line: `_dispatch(name, params)`, the single API choke point.
"""

from __future__ import annotations

from datetime import timedelta

from kiteconnect import KiteConnect
from kiteconnect import exceptions as kex

from executor.brokers.kitesim import gtt as gtts
from executor.brokers.kitesim import rules
from executor.brokers.kitesim.engine import WORKING, SimEngine
from executor.brokers.kitesim.payloads import order_view, profile, ts

GTT_LIFETIME = timedelta(days=365)


def _params(loc: dict) -> dict:
    return {k: v for k, v in loc.items() if k != "self"}


class KiteSim(SimEngine):
    """Zerodha Kite, simulated in-process. See the package docstring."""

    # ── orders ────────────────────────────────────────────────────────────────

    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product,
                    order_type, price=None, validity=None, validity_ttl=None,
                    disclosed_quantity=None, trigger_price=None, iceberg_legs=None,
                    iceberg_quantity=None, auction_number=None, algo_id=None, tag=None,
                    market_protection=None):  # fmt: skip
        return self._dispatch("place_order", _params(locals()))

    def modify_order(self, variety, order_id, parent_order_id=None, quantity=None, price=None,
                     order_type=None, trigger_price=None, validity=None,
                     disclosed_quantity=None, market_protection=None):  # fmt: skip
        return self._dispatch("modify_order", _params(locals()))

    def cancel_order(self, variety, order_id, parent_order_id=None):
        return self._dispatch("cancel_order", _params(locals()))

    def orders(self):
        return self._dispatch("orders", {})

    def order_history(self, order_id):
        return self._dispatch("order_history", _params(locals()))

    # ── portfolio, funds, market data ─────────────────────────────────────────

    def positions(self):
        return self._dispatch("positions", {})

    def holdings(self):
        return self._dispatch("holdings", {})

    def margins(self, segment=None):
        return self._dispatch("margins", _params(locals()))

    def ltp(self, *instruments):
        return self._dispatch("ltp", {"instruments": instruments})

    def instruments(self, exchange=None):
        return self._dispatch("instruments", _params(locals()))

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False,
                        oi=False):  # fmt: skip
        return self._dispatch("historical_data", _params(locals()))

    # ── GTT ───────────────────────────────────────────────────────────────────

    def place_gtt(self, trigger_type, tradingsymbol, exchange, trigger_values, last_price, orders):
        return self._dispatch("place_gtt", _params(locals()))

    def modify_gtt(self, trigger_id, trigger_type, tradingsymbol, exchange, trigger_values,
                   last_price, orders):  # fmt: skip
        return self._dispatch("modify_gtt", _params(locals()))

    def delete_gtt(self, trigger_id):
        return self._dispatch("delete_gtt", _params(locals()))

    def get_gtt(self, trigger_id):
        return self._dispatch("get_gtt", _params(locals()))

    def get_gtts(self):
        return self._dispatch("get_gtts", {})

    # ── session ───────────────────────────────────────────────────────────────

    def profile(self):
        return self._dispatch("profile", {})

    def set_access_token(self, access_token):
        return self._dispatch("set_access_token", _params(locals()))

    # ── implementations (always under the lock, via _dispatch) ────────────────

    def _api_place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity,
                         product, order_type, price=None, validity=None, validity_ttl=None,
                         disclosed_quantity=None, trigger_price=None, iceberg_legs=None,
                         iceberg_quantity=None, auction_number=None, algo_id=None, tag=None,
                         market_protection=None):  # fmt: skip
        if variety != KiteConnect.VARIETY_REGULAR:
            raise kex.InputException(f"Variety {variety!r} is not supported by the simulator.")
        tag = tag or None  # an empty tag is no tag
        if tag is not None and (len(str(tag)) > 20 or not str(tag).isalnum()):
            raise kex.InputException("Invalid `tag`: alphanumeric, at most 20 characters.")
        row = self._instrument(tradingsymbol, exchange)
        params = {
            "variety": variety,
            "exchange": exchange,
            "symbol": tradingsymbol,
            "side": transaction_type,
            "order_type": order_type,
            "product": product,
            "quantity": quantity,
            "price": float(price or 0.0),
            "trigger_price": float(trigger_price or 0.0),
            "validity": validity or KiteConnect.VALIDITY_DAY,
            "tag": tag,
            "market_protection": market_protection,
        }
        check = {**params, "transaction_type": transaction_type}
        err = rules.input_error(check, float(row["tick_size"]), self._ltp.get(_key(row, exchange)))
        if err:
            raise kex.InputException(err)
        return self._create(params, row)["order_id"]

    def _api_modify_order(self, variety, order_id, parent_order_id=None, quantity=None,
                          price=None, order_type=None, trigger_price=None, validity=None,
                          disclosed_quantity=None, market_protection=None):  # fmt: skip
        o = self._order(order_id)
        if o["status"] not in WORKING:
            raise kex.InputException(f"Order cannot be modified as it is {o['status'].lower()}.")
        changes = {
            k: v
            for k, v in {
                "quantity": quantity,
                "price": None if price is None else float(price),
                "order_type": order_type,
                "trigger_price": None if trigger_price is None else float(trigger_price),
                "validity": validity,
                "market_protection": market_protection,
            }.items()
            if v is not None
        }
        proposed = {**o, **changes}
        if proposed["quantity"] <= o["filled_quantity"]:
            raise kex.InputException("Quantity can't be less than or equal to the filled quantity.")
        row = self._instrument(o["tradingsymbol"], o["exchange"])
        ltp = self._ltp.get(_key(row, o["exchange"]))
        side_ltp = ltp if o["status"] == "TRIGGER PENDING" else None  # triggered: no side rule
        err = rules.input_error(proposed, float(row["tick_size"]), side_ltp)
        if err:
            raise kex.InputException(err)
        if "market_protection" in changes:
            changes["market_protection"] = float(changes["market_protection"])
        o["_per_share"] *= _ratio(
            rules.reference_price(proposed, ltp), rules.reference_price(o, ltp)
        )
        self._advance(
            o,
            o["status"],
            modified=True,
            pending_quantity=proposed["quantity"] - o["filled_quantity"],
            **changes,
        )
        if ltp is not None:
            self._match(o, ltp, continuous=False, fresh=True)
        return o["order_id"]

    def _api_cancel_order(self, variety, order_id, parent_order_id=None):
        o = self._order(order_id)
        if o["status"] not in WORKING:
            raise kex.InputException(f"Order cannot be cancelled as it is {o['status'].lower()}.")
        self._advance(o, "CANCELLED", cancelled_quantity=o["pending_quantity"], pending_quantity=0)
        return o["order_id"]

    def _api_orders(self):
        return [order_view(o) for o in sorted(self._orders.values(), key=lambda o: o["_seq"])]

    def _api_order_history(self, order_id):
        self._order(order_id)
        return [order_view(s) for s in self._history.get(str(order_id), [])]

    def _api_positions(self):
        rows = [
            p.view(self._ltp.get(f"{p.exchange}:{p.tradingsymbol}"),
                   self._close.get(f"{p.exchange}:{p.tradingsymbol}"))
            for p in self._book.positions.values()
        ]  # fmt: skip
        return {"net": rows, "day": [dict(r) for r in rows]}

    def _api_holdings(self):
        return [dict(h) for h in self.holdings_book]

    def _api_margins(self, segment=None):
        equity = self._book.margins(self._order_blocked(), self._ltp)
        if segment == KiteConnect.MARGIN_EQUITY:
            return equity
        if segment is None:
            return {"equity": equity}
        raise kex.InputException(f"Invalid segment: {segment}.")

    def _api_ltp(self, instruments):
        keys = list(instruments)
        if keys and isinstance(keys[0], list):  # the SDK accepts a list (legacy)
            keys = keys[0]
        out = {}
        for k in keys:
            exchange, _, symbol = str(k).partition(":")
            if k in self._ltp and (row := self._instruments.row(symbol, exchange)):
                out[k] = {
                    "instrument_token": int(row["instrument_token"]),
                    "last_price": self._ltp[k],
                }
        return out

    def _api_instruments(self, exchange=None):
        return self._instruments.rows(exchange or KiteConnect.EXCHANGE_NSE)

    def _api_historical_data(self, instrument_token, from_date, to_date, interval,
                             continuous=False, oi=False):  # fmt: skip
        return []  # bar data reaches the simulator through on_bar / on_price

    def _api_place_gtt(self, trigger_type, tradingsymbol, exchange, trigger_values, last_price,
                       orders):  # fmt: skip
        row = self._instrument(tradingsymbol, exchange)
        gtts.validate(trigger_type, trigger_values, last_price, orders, float(row["tick_size"]))
        self._gtt_seq += 1
        gid = 100000 + self._gtt_seq
        now = self._now()
        self._gtts[gid] = gtts.build(
            gid, self.config.user_id, trigger_type, tradingsymbol, exchange,
            int(row["instrument_token"]), trigger_values, last_price, orders,
            ts(now), ts(now + GTT_LIFETIME),
        )  # fmt: skip
        self._dirty = True
        return {"trigger_id": gid}

    def _api_modify_gtt(self, trigger_id, trigger_type, tradingsymbol, exchange, trigger_values,
                        last_price, orders):  # fmt: skip
        g = self._gtt(trigger_id)
        if g["status"] != "active":
            raise kex.InputException(f"Trigger cannot be modified as it is {g['status']}.")
        row = self._instrument(tradingsymbol, exchange)
        gtts.validate(trigger_type, trigger_values, last_price, orders, float(row["tick_size"]))
        fresh = gtts.build(
            g["id"], g["user_id"], trigger_type, tradingsymbol, exchange,
            int(row["instrument_token"]), trigger_values, last_price, orders,
            g["created_at"], g["expires_at"],
        )  # fmt: skip
        fresh["updated_at"] = ts(self._now())
        self._gtts[g["id"]] = fresh
        self._dirty = True
        return {"trigger_id": g["id"]}

    def _api_delete_gtt(self, trigger_id):
        g = self._gtt(trigger_id)
        if g["status"] != "active":
            raise kex.InputException(f"Trigger cannot be deleted as it is {g['status']}.")
        g["status"], g["updated_at"] = "deleted", ts(self._now())
        self._dirty = True
        return {"trigger_id": g["id"]}

    def _api_get_gtt(self, trigger_id):
        return gtts.view(self._gtt(trigger_id))

    def _api_get_gtts(self):
        return [gtts.view(g) for g in self._gtts.values()]

    def _api_profile(self):
        return profile(self.config.user_id)

    def _api_set_access_token(self, access_token):
        self.access_token = access_token

    def _gtt(self, trigger_id) -> dict:
        g = self._gtts.get(int(trigger_id))
        if g is None:
            raise kex.InputException("Invalid trigger ID.")
        return g


def _key(row: dict, exchange: str) -> str:
    return f"{exchange}:{row['tradingsymbol']}"


def _ratio(new: float | None, old: float | None) -> float:
    return (new / old) if new and old else 1.0


# The SDK's constants (VARIETY_REGULAR, GTT_TYPE_SINGLE, ...), as KiteBroker reads them.
for _name in dir(KiteConnect):
    if _name.isupper():
        setattr(KiteSim, _name, getattr(KiteConnect, _name))

__all__ = ["KiteSim"]
