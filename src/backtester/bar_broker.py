"""The backtester's broker: the real KiteBroker on KiteSim, driven by daily bars (#86).

Same code path as paper and live: orders go through KiteBroker's translation
layer into KiteSim, which walks each bar open -> nearer extreme -> farther
extreme -> close. The backtester keeps its own cash ledger (costs and slippage
are its job), so KiteSim's margin and circuit checks are off, as the pre-#86 simulator had
none.

Entries are decided with the day's bar in hand (the existing design) and placed
at the bar's open: `enter()` rewinds the instrument to the open, places the
order, then walks the rest of the bar. A MARKET entry fills at the open; a BUY
LIMIT at or above the open fills at the open, below it when the walk reaches it.
Orders still working at the close expire (`end_day`), like Kite's DAY validity.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from executor.brokers.kite_broker import KiteBroker
from executor.brokers.kitesim import InstrumentBook, KiteSim, KiteSimConfig, bar_path
from executor.models import BrokerName, GttRequest, OrderRequest, PriceBar

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)


class _SimClock:
    def __init__(self) -> None:
        self.now = datetime.combine(date(2000, 1, 3), MARKET_OPEN, IST)

    def __call__(self) -> datetime:
        return self.now


class BarBroker:
    def __init__(self, cash: float) -> None:
        self.clock = _SimClock()
        self.sim = KiteSim(
            instruments=InstrumentBook(None),  # no dump in a backtest: 0.05 tick
            cash=cash,
            clock=self.clock,
            config=KiteSimConfig(check_margin=False, circuit_pct=None),
        )
        self.broker = KiteBroker(kite=self.sim, broker_name=BrokerName.PAPER, clock=self.clock)

    def start_day(self, day: date) -> None:
        self.clock.now = datetime.combine(day, MARKET_OPEN, IST)

    def walk(self, bar: PriceBar) -> None:
        self.sim.on_bar(bar)

    def enter(self, request: OrderRequest, bar: PriceBar) -> dict:
        """Place `request` at the bar's open and walk the bar; the broker's status."""
        path = bar_path(bar.open, bar.high, bar.low, bar.close)
        self.sim.on_price(bar.symbol, request.exchange, path[0])
        oid = self.broker.place_order(request)
        for p in path[1:]:
            self.sim.on_price(bar.symbol, request.exchange, p, continuous=True)
        return self.broker.get_order_status(oid)

    def place_gtt(self, request: GttRequest) -> str:
        return self.broker.place_gtt(request)

    def triggered_gtts(self) -> list[tuple[str, int, float]]:
        """(trigger_id, leg index, leg price) of every GTT that has fired."""
        out = []
        for g in self.broker.list_gtts():
            if g["status"] != "triggered":
                continue
            for i, leg in enumerate(g["orders"]):
                if leg.get("result"):
                    out.append((str(g["id"]), i, float(leg["price"])))
        return out

    def end_day(self) -> None:
        self.sim.end_of_day()
