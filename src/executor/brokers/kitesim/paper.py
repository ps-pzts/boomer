"""Paper mode's broker: the real KiteBroker on KiteSim (#86).

    broker = build_paper_broker(feed, cash, instruments=live.instruments.rows, book=PaperBook(...))

* prices: every tick of the live market feed (SimFeed)
* instruments / tick sizes: the live KiteBroker's instrument dump when there is
  one; otherwise stand-in rows on the 0.05 default tick
* orders labelled `paper` in our tables (KiteBroker's broker_name)
* persistence (#64): the simulator's whole state is saved to the paper book after
  every change and restored at build; a pre-#86 row is ignored (logged)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.brokers.feeds.base import MarketDataFeed
from executor.brokers.kite_broker import KiteBroker
from executor.brokers.kitesim.config import KiteSimConfig
from executor.brokers.kitesim.feed import SimFeed
from executor.brokers.kitesim.instruments import InstrumentBook
from executor.brokers.kitesim.sim import KiteSim
from executor.models import BrokerName

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _ist_now() -> datetime:
    return datetime.now(IST)


def build_paper_broker(
    feed: MarketDataFeed,
    cash: float,
    *,
    instruments: InstrumentBook | Callable[[str], list[dict]] | None = None,
    book: object | None = None,
    clock: Callable[[], datetime] | None = None,
    config: KiteSimConfig | None = None,
) -> KiteBroker:
    if not isinstance(instruments, InstrumentBook):
        instruments = InstrumentBook(instruments, synthesize_unknown=instruments is None)
    sim = KiteSim(
        instruments=instruments,
        cash=cash,
        clock=clock or _ist_now,
        config=config,
    )
    if book is not None:
        if sim.load_state(book.load()):  # type: ignore[attr-defined]
            logger.info("paper_book_restored")
        sim.on_change = book.save  # type: ignore[attr-defined]
    return KiteBroker(feed=SimFeed(feed, sim), kite=sim, broker_name=BrokerName.PAPER)
