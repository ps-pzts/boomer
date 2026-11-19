"""#64: the paper broker's book survives a mid-session restart."""

from __future__ import annotations

from datetime import date

from executor.brokers.paper_book import PaperBook
from executor.brokers.paper_broker import PaperBroker
from executor.models import BrokerName
from executor.order_manager import OrderManager
from executor.position_manager import PositionManager
from executor.session_inputs import net_by_symbol
from executor.trade_lifecycle import TradeSupervisor
from tests.executor.test_trade_lifecycle import Harness, _filled_and_protected

DAY = date(2026, 11, 19)


def _db_path(h: Harness) -> str:
    return h.db.execute("PRAGMA database_list").fetchone()[2]


def _with_book(h: Harness) -> Harness:
    """Swap the harness's paper broker for one that persists its book."""
    h.paper = PaperBroker(
        price_source=h.feed, initial_cash=1_000_000, book=PaperBook(_db_path(h), DAY)
    )
    h.om = OrderManager(h.db, {BrokerName.KITE: h.paper}, ltp_cache={}, clock=h.clock)
    h.pm = PositionManager(h.db, gtt_manager=None, order_manager=h.om)
    h.sup = TradeSupervisor(h.db, h.om, h.pm, h.alerter, clock=h.clock)
    return h


def _restart(h: Harness) -> Harness:
    """A new process: fresh broker objects, same DB, same feed."""
    return _with_book(h)


def test_round_trip(tmp_path):
    h = Harness(tmp_path)
    book = PaperBook(_db_path(h), DAY)
    assert book.load() is None
    book.save({"cash": 5.0, "orders": {}, "positions": {}})
    assert book.load() == {"cash": 5.0, "orders": {}, "positions": {}}
    assert PaperBook(_db_path(h), date(2026, 11, 20)).load() is None  # per day


def test_save_failure_never_raises(tmp_path):
    PaperBook(str(tmp_path / "missing-dir" / "x.db"), DAY).save({"cash": 1})


def test_open_trade_survives_a_restart(tmp_path):
    h = _with_book(Harness(tmp_path))
    bid = _filled_and_protected(h)
    before = net_by_symbol(h.paper.list_positions())
    h = _restart(h)
    assert net_by_symbol(h.paper.list_positions()) == before == {"INFY": 10}
    assert h.sup.net_positions() == {"INFY": 10}  # reconcile check would be clean
    stop = h.om.sync_status(h.bracket(bid)["stop_order_id"])
    assert stop.status == "pending"  # the restored stop is still resting
    h.tick(97.5)  # and still fires
    h.sup.reconcile()
    h.sup.reconcile()
    b = h.bracket(bid)
    assert (b["state"], b["exit_reason"]) == ("closed", "stop_hit")
    assert h.paper.list_positions() == []


def test_cash_restored(tmp_path):
    h = _with_book(Harness(tmp_path))
    _filled_and_protected(h)
    cash = h.paper.get_funds().available_cash
    assert cash == 1_000_000 - 10 * 100.0
    assert _restart(h).paper.get_funds().available_cash == cash
