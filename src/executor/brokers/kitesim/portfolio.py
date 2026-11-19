"""Positions and margins for KiteSim, shaped like Kite's `positions()` / `margins()`.

One position per (exchange, symbol, product). The average price moves only when
exposure is added and resets to the fill price when the position flips through
zero (the pre-#86 simulator's rule, #40); closing quantity books realised P&L against it.

Margins (Kite's `margins(segment="equity")`):
  available.cash      opening balance; Kite does not move it intraday
  utilised.debits     margin blocked by open positions and working orders
                      + realised and unrealised losses
  net / live_balance  cash - debits: what new orders may use
Realised profits are not credited intraday (conservative; Kite settles them T+1).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Position:
    exchange: str
    tradingsymbol: str
    instrument_token: int
    product: str
    quantity: int = 0
    average_price: float = 0.0
    realised: float = 0.0
    buy_quantity: int = 0
    buy_value: float = 0.0
    sell_quantity: int = 0
    sell_value: float = 0.0

    def apply(self, side: str, qty: int, price: float) -> None:
        signed = qty if side == "BUY" else -qty
        if side == "BUY":
            self.buy_quantity += qty
            self.buy_value += qty * price
        else:
            self.sell_quantity += qty
            self.sell_value += qty * price
        old = self.quantity
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):  # opening or adding
            self.average_price = (self.average_price * abs(old) + price * qty) / abs(new)
        else:
            closed = min(abs(old), qty)
            direction = 1 if old > 0 else -1
            self.realised += (price - self.average_price) * closed * direction
            if new == 0:
                self.average_price = 0.0
            elif (old > 0) != (new > 0):  # flipped through zero
                self.average_price = price
        self.quantity = new

    def unrealised(self, last: float) -> float:
        return (last - self.average_price) * self.quantity if self.quantity else 0.0

    def view(self, last: float | None, close: float | None) -> dict:
        last_price = float(last if last is not None else self.average_price)
        unreal = self.unrealised(last_price)
        pnl = self.realised + unreal
        bq, sq = self.buy_quantity, self.sell_quantity
        bp = self.buy_value / bq if bq else 0.0
        sp = self.sell_value / sq if sq else 0.0
        return {
            "tradingsymbol": self.tradingsymbol,
            "exchange": self.exchange,
            "instrument_token": self.instrument_token,
            "product": self.product,
            "quantity": self.quantity,
            "overnight_quantity": 0,
            "multiplier": 1,
            "average_price": round(self.average_price, 4),
            "close_price": float(close or 0.0),
            "last_price": last_price,
            "value": round(self.sell_value - self.buy_value, 4),
            "pnl": round(pnl, 4),
            "m2m": round(pnl, 4),
            "unrealised": round(unreal, 4),
            "realised": round(self.realised, 4),
            "buy_quantity": bq,
            "buy_price": round(bp, 4),
            "buy_value": round(self.buy_value, 4),
            "buy_m2m": round(self.buy_value, 4),
            "sell_quantity": sq,
            "sell_price": round(sp, 4),
            "sell_value": round(self.sell_value, 4),
            "sell_m2m": round(self.sell_value, 4),
            "day_buy_quantity": bq,
            "day_buy_price": round(bp, 4),
            "day_buy_value": round(self.buy_value, 4),
            "day_sell_quantity": sq,
            "day_sell_price": round(sp, 4),
            "day_sell_value": round(self.sell_value, 4),
        }


def added_exposure(current: int, side: str, qty: int) -> int:
    """Shares of this order that open or add to a position (the rest close it)."""
    signed = qty if side == "BUY" else -qty
    if current == 0 or (current > 0) == (signed > 0):
        return qty
    return max(0, qty - abs(current))


class Portfolio:
    def __init__(self, cash: float, mis_rate: float, cnc_rate: float) -> None:
        self.cash = float(cash)
        self.mis_rate = mis_rate
        self.cnc_rate = cnc_rate
        self.positions: dict[str, Position] = {}

    @staticmethod
    def key(exchange: str, symbol: str, product: str) -> str:
        return f"{exchange}:{symbol}:{product}"

    def position(self, exchange: str, symbol: str, product: str, token: int) -> Position:
        k = self.key(exchange, symbol, product)
        if k not in self.positions:
            self.positions[k] = Position(exchange, symbol, token, product)
        return self.positions[k]

    def net_quantity(self, exchange: str, symbol: str, product: str) -> int:
        p = self.positions.get(self.key(exchange, symbol, product))
        return p.quantity if p else 0

    def rate(self, product: str) -> float:
        return self.cnc_rate if product == "CNC" else self.mis_rate

    def blocked(self) -> float:
        return sum(
            abs(p.quantity) * p.average_price * self.rate(p.product)
            for p in self.positions.values()
        )

    def margins(self, order_blocked: float, ltp: dict[str, float]) -> dict:
        realised = sum(p.realised for p in self.positions.values())
        unrealised = sum(
            p.unrealised(ltp.get(f"{p.exchange}:{p.tradingsymbol}", p.average_price))
            for p in self.positions.values()
        )
        exposure = self.blocked() + order_blocked
        debits = exposure + max(0.0, -realised) + max(0.0, -unrealised)
        net = self.cash - debits
        delivery = sum(
            abs(p.quantity) * p.average_price for p in self.positions.values() if p.product == "CNC"
        )
        return {
            "enabled": True,
            "net": round(net, 2),
            "available": {
                "adhoc_margin": 0.0,
                "cash": round(self.cash, 2),
                "opening_balance": round(self.cash, 2),
                "live_balance": round(net, 2),
                "collateral": 0.0,
                "intraday_payin": 0.0,
            },
            "utilised": {
                "debits": round(debits, 2),
                "exposure": round(exposure, 2),
                "m2m_realised": round(realised, 2),
                "m2m_unrealised": round(unrealised, 2),
                "option_premium": 0.0,
                "payout": 0.0,
                "span": 0.0,
                "holding_sales": 0.0,
                "turnover": 0.0,
                "liquid_collateral": 0.0,
                "stock_collateral": 0.0,
                "equity": 0.0,
                "delivery": round(delivery, 2),
            },
        }

    def available(self, order_blocked: float, ltp: dict[str, float]) -> float:
        return self.margins(order_blocked, ltp)["net"]

    def to_state(self) -> dict:
        return {"cash": self.cash, "positions": {k: asdict(p) for k, p in self.positions.items()}}

    def load_state(self, state: dict) -> None:
        self.cash = float(state["cash"])
        self.positions = {k: Position(**v) for k, v in state["positions"].items()}
