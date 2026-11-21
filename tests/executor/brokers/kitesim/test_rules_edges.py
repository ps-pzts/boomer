"""KiteSim rules, edge by edge (#89): the pure functions at their exact boundaries.

Numbers are hand-checkable: circuit band 25% around 100 is 75..125; margin 100.00 needed
against 100.00 available just fits; a SELL stop at 100 is crossed by a price of exactly 100.
"""

from __future__ import annotations

import pytest

from executor.brokers.kitesim import rules


def _o(**kw) -> dict:
    base = {
        "order_type": "LIMIT", "transaction_type": "BUY", "product": "MIS", "validity": "DAY",
        "quantity": 1, "price": 100.0, "trigger_price": 0.0, "market_protection": None,
    }  # fmt: skip
    return {**base, **kw}


class TestOnTick:
    def test_multiples(self):
        assert rules.on_tick(100.05, 0.05)
        assert not rules.on_tick(100.07, 0.05)

    def test_zero_value_is_always_on_tick(self):
        assert rules.on_tick(0, 0.05)

    @pytest.mark.parametrize("tick", [0, -0.05])
    def test_no_usable_tick_accepts_anything(self, tick):
        assert rules.on_tick(100.07, tick)


class TestInputErrorMessages:
    @pytest.mark.parametrize(
        "field, value, message",
        [
            ("order_type", "STOP", "Invalid `order_type`: STOP."),
            ("transaction_type", "HOLD", "Invalid `transaction_type`: HOLD."),
            ("product", "MTF", "Invalid `product`: MTF."),
            ("validity", "GTC", "Invalid `validity`: GTC."),
        ],
    )
    def test_enum_fields(self, field, value, message):
        assert rules.input_error(_o(**{field: value}), 0.05, None) == message

    @pytest.mark.parametrize("qty", [0, -3, 1.5, True, "5"])
    def test_bad_quantity(self, qty):
        assert rules.input_error(_o(quantity=qty), 0.05, None) == "Invalid `quantity`."

    def test_good_quantity(self):
        assert rules.input_error(_o(quantity=1), 0.05, None) is None
        assert rules.input_error(_o(quantity=500), 0.05, None) is None

    def test_limit_price_must_be_positive_but_may_be_small(self):
        assert rules.input_error(_o(price=0.0), 0.05, None) == "Invalid `price`."
        assert rules.input_error(_o(price=-1.0), 0.05, None) == "Invalid `price`."
        assert rules.input_error(_o(price=0.5), 0.05, None) is None  # below 1 is a real price

    def test_stop_trigger_must_be_positive_but_may_be_small(self):
        sl = _o(order_type="SL", transaction_type="SELL", price=0.5, trigger_price=0.5)
        assert rules.input_error({**sl, "trigger_price": 0.0}, 0.05, None) == (
            "Invalid `trigger_price`."
        )
        assert rules.input_error(sl, 0.05, None) is None

    def test_market_orders_need_protection(self):
        m = _o(order_type="MARKET", price=0.0)
        msg = "Market orders without market protection are not allowed via API."
        assert rules.input_error(m, 0.05, None).startswith(msg)
        assert rules.input_error({**m, "market_protection": 0}, 0.05, None).startswith(msg)
        assert rules.input_error({**m, "market_protection": 1}, 0.05, None) is None
        assert rules.input_error({**m, "market_protection": -1}, 0.05, None) is None


class TestStopSide:
    def _sl(self, side, price, trig):
        return _o(order_type="SL", transaction_type=side, price=price, trigger_price=trig)

    def test_sell_limit_above_trigger_is_refused(self):
        msg = rules.stop_side_error(self._sl("SELL", 101.0, 100.0), None)
        assert msg == "Trigger price for stoploss sell orders cannot be lower than the limit price."

    def test_sell_limit_equal_to_trigger_is_fine(self):
        assert rules.stop_side_error(self._sl("SELL", 100.0, 100.0), None) is None
        assert rules.stop_side_error(self._sl("SELL", 99.0, 100.0), None) is None

    def test_buy_limit_below_trigger_is_refused(self):
        msg = rules.stop_side_error(self._sl("BUY", 99.0, 100.0), None)
        assert msg == "Trigger price for stoploss buy orders cannot be higher than the limit price."

    def test_buy_limit_equal_to_trigger_is_fine(self):
        assert rules.stop_side_error(self._sl("BUY", 100.0, 100.0), None) is None
        assert rules.stop_side_error(self._sl("BUY", 101.0, 100.0), None) is None

    def test_trigger_through_the_market(self):
        sell = _o(order_type="SL-M", transaction_type="SELL", trigger_price=100.0)
        assert rules.stop_side_error(sell, 100.0) == rules.WRONG_SIDE["SELL"]  # equal is through
        assert rules.stop_side_error(sell, 100.05) is None
        buy = _o(order_type="SL-M", transaction_type="BUY", trigger_price=100.0)
        assert rules.stop_side_error(buy, 100.0) == rules.WRONG_SIDE["BUY"]
        assert rules.stop_side_error(buy, 99.95) is None

    def test_non_stop_orders_have_no_side_rule(self):
        assert rules.stop_side_error(_o(), 50.0) is None


class TestReferencePrice:
    def test_by_order_type(self):
        assert rules.reference_price(_o(order_type="LIMIT", price=101.0), 99.0) == 101.0
        assert rules.reference_price(_o(order_type="SL", price=101.0), 99.0) == 101.0
        assert rules.reference_price(_o(order_type="SL-M", trigger_price=97.0), 99.0) == 97.0
        assert rules.reference_price(_o(order_type="MARKET"), 99.0) == 99.0
        assert rules.reference_price(_o(order_type="MARKET"), None) is None


class TestCircuit:
    # ref 100, band 25%: 75 .. 125
    def test_inside_and_on_the_band_edge(self):
        assert rules.circuit_rejection(_o(price=75.0), 100.0, 0.25) is None
        assert rules.circuit_rejection(_o(price=125.0), 100.0, 0.25) is None
        assert rules.circuit_rejection(_o(price=100.0), 100.0, 0.25) is None

    def test_outside_the_band(self):
        low = rules.circuit_rejection(_o(price=74.95), 100.0, 0.25)
        assert "lower circuit limit of 75.00" in low
        high = rules.circuit_rejection(_o(price=125.05), 100.0, 0.25)
        assert "upper circuit limit of 125.00" in high

    def test_no_band_or_no_reference_or_market_order(self):
        assert rules.circuit_rejection(_o(price=1.0), 100.0, None) is None
        assert rules.circuit_rejection(_o(price=1.0), None, 0.25) is None
        assert rules.circuit_rejection(_o(order_type="MARKET", price=1.0), 100.0, 0.25) is None


class TestMargin:
    def test_exactly_enough_fits(self):
        assert rules.margin_rejection(100.0, 100.0, "AB1234") is None

    def test_float_dust_is_tolerated_both_ways(self):
        assert rules.margin_rejection(100.0 + 5e-10, 100.0, "AB1234") is None
        assert rules.margin_rejection(100.0 - 5e-10, 100.0, "AB1234") is None

    def test_real_shortfall_is_refused_with_kites_text(self):
        short, rms = rules.margin_rejection(100.5, 100.0, "AB1234")
        assert short == (
            "Insufficient funds. Required margin is 100.50 but available margin is 100.00. "
            "Check the orderbook for open orders."
        )
        assert rms.startswith("RMS:Margin Exceeds,Required:100.50, Available:100.00 for entity")
        assert "account-AB1234" in rms
        assert rules.margin_rejection(100.00001, 100.0, "AB1234") is not None


class TestFreeze:
    def test_freeze_limit_is_inclusive(self):
        assert rules.freeze_rejection(1800, 1800) is None
        assert rules.freeze_rejection(1801, 1800) == (
            "Quantity exceeds the freeze limit of 1800. Split the order and try again."
        )
        assert rules.freeze_rejection(10**9, None) is None


class TestMatching:
    def test_stop_is_crossed_at_exactly_the_trigger(self):
        assert rules.stop_crossed("SELL", 100.0, 100.0)
        assert rules.stop_crossed("SELL", 100.0, 99.95)
        assert not rules.stop_crossed("SELL", 100.0, 100.05)
        assert rules.stop_crossed("BUY", 100.0, 100.0)
        assert rules.stop_crossed("BUY", 100.0, 100.05)
        assert not rules.stop_crossed("BUY", 100.0, 99.95)

    def test_stop_fill_price(self):
        assert rules.stop_fill_price("SELL", 100.0, 95.0, continuous=True) == 100.0
        assert rules.stop_fill_price("SELL", 100.0, 95.0, continuous=False) == 95.0  # gap: worse
        assert rules.stop_fill_price("SELL", 100.0, 100.0, continuous=False) == 100.0
        assert rules.stop_fill_price("BUY", 100.0, 105.0, continuous=False) == 105.0
        assert rules.stop_fill_price("BUY", 100.0, 100.0, continuous=True) == 100.0

    def test_limit_marketable_inclusive(self):
        assert rules.limit_marketable("BUY", 100.0, 100.0)
        assert rules.limit_marketable("BUY", 100.0, 99.95)
        assert not rules.limit_marketable("BUY", 100.0, 100.05)
        assert rules.limit_marketable("SELL", 100.0, 100.0)
        assert not rules.limit_marketable("SELL", 100.0, 99.95)
