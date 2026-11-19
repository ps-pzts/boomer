"""KiteSim's instrument book and portfolio maths (#86), with hand-checked numbers."""

from __future__ import annotations

import pytest

from executor.brokers.kite_contract import check_shape
from executor.brokers.kitesim.instruments import DEFAULT_TICK, InstrumentBook, synthetic_row
from executor.brokers.kitesim.portfolio import Portfolio, Position, added_exposure
from tests import kite_fixtures as kf


class TestInstrumentBook:
    def test_rows_and_row_from_the_dump(self):
        calls: list[str] = []

        def loader(exchange):
            calls.append(exchange)
            return [dict(r) for r in kf.sdk("instruments")]

        book = InstrumentBook(loader)
        assert book.row("INFY", "NSE")["tick_size"] == 0.1
        assert {r["tradingsymbol"] for r in book.rows("NSE")} >= {"INFY", "KPEL"}
        assert book.row("NOPE", "NSE") is None  # Kite would refuse it
        assert calls == ["NSE"]  # loaded once

    def test_synthesizes_unknown_when_asked(self):
        book = InstrumentBook(lambda ex: [], synthesize_unknown=True)
        row = book.row("TCS", "NSE")
        assert (row["tick_size"], row["tradingsymbol"]) == (DEFAULT_TICK, "TCS")

    def test_no_loader_means_every_row_is_a_stand_in(self):
        assert InstrumentBook(None).row("ANY", "NSE")["tick_size"] == DEFAULT_TICK

    def test_a_dump_that_fails_to_load_falls_back(self, caplog):
        def broken(exchange):
            raise RuntimeError("not authenticated")

        book = InstrumentBook(broken)
        assert book.row("INFY", "NSE")["tick_size"] == DEFAULT_TICK
        assert "kitesim_instruments_unavailable" in caplog.text

    def test_synthetic_row_is_dump_shaped_and_stable(self):
        row = synthetic_row("TCS", "NSE")
        assert check_shape("instrument", row) == []
        assert row["instrument_token"] == synthetic_row("TCS", "NSE")["instrument_token"]


class TestPosition:
    def test_average_moves_only_when_adding(self):
        p = Position("NSE", "INFY", 1, "MIS")
        p.apply("BUY", 10, 100.0)
        p.apply("BUY", 10, 110.0)
        assert (p.quantity, p.average_price) == (20, 105.0)
        p.apply("SELL", 5, 120.0)  # books (120 - 105) x 5 = 75
        assert (p.quantity, p.average_price, p.realised) == (15, 105.0, 75.0)

    def test_short_side_and_flip(self):
        p = Position("NSE", "INFY", 1, "MIS")
        p.apply("SELL", 10, 100.0)
        p.apply("BUY", 15, 90.0)  # covers 10 at +10 each, then long 5 @ 90
        assert (p.quantity, p.average_price, p.realised) == (5, 90.0, 100.0)

    def test_view_matches_the_contract(self):
        p = Position("NSE", "INFY", 408065, "MIS")
        p.apply("BUY", 10, 1502.3)
        row = p.view(1510.15, 1498.4)
        assert check_shape("position", row) == []
        assert (row["value"], row["unrealised"]) == (-15023.0, pytest.approx(78.5))

    @pytest.mark.parametrize(
        ("held", "side", "qty", "adds"),
        [(0, "BUY", 10, 10), (10, "BUY", 5, 5), (10, "SELL", 10, 0), (10, "SELL", 15, 5),
         (-10, "BUY", 4, 0)],
    )  # fmt: skip
    def test_added_exposure(self, held, side, qty, adds):
        assert added_exposure(held, side, qty) == adds


class TestPortfolio:
    def test_margins_worked_example(self):
        """Cash 100,000; long 10 MIS @ 100 (margin 200) and 5 CNC @ 200 (1,000);
        a working order blocks 300 more; INFY last 95 -> unrealised -50.
        debits = 200 + 1,000 + 300 + 50 = 1,550; net = 98,450."""
        book = Portfolio(100_000.0, 0.20, 1.0)
        book.position("NSE", "INFY", "MIS", 1).apply("BUY", 10, 100.0)
        book.position("NSE", "KPEL", "CNC", 2).apply("BUY", 5, 200.0)
        m = book.margins(300.0, {"NSE:INFY": 95.0, "NSE:KPEL": 200.0})
        assert check_shape("margins_equity", m) == []
        assert m["utilised"]["debits"] == 1_550.0
        assert m["net"] == m["available"]["live_balance"] == 98_450.0
        assert m["available"]["cash"] == 100_000.0
        assert m["utilised"]["delivery"] == 1_000.0
        assert book.available(300.0, {"NSE:INFY": 95.0, "NSE:KPEL": 200.0}) == 98_450.0

    def test_state_round_trip(self):
        book = Portfolio(5_000.0, 0.2, 1.0)
        book.position("NSE", "INFY", "MIS", 1).apply("BUY", 3, 100.0)
        twin = Portfolio(0.0, 0.2, 1.0)
        twin.load_state(book.to_state())
        assert twin.to_state() == book.to_state()
        assert twin.net_quantity("NSE", "INFY", "MIS") == 3
