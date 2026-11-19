"""KiteSim for tests (#86): fixture instruments, a contract-checked SDK view, and
the paper KiteBroker built exactly as paper mode builds it.

    clock = Clock()                          # fixed 2026-11-19 10:00 IST, settable
    sim = make_sim(clock=clock)              # fixture CSV instruments (+ stand-ins)
    broker = paper_broker(sim, feed)         # KiteBroker(kite=CheckedKite(sim), PAPER)
    sim.on_price("INFY", "NSE", 100.0)

`CheckedKite` binds every call to `kiteconnect.KiteConnect`'s own signature and
runs every result through `kite_contract.check_shape`, so the simulator can never
hand KiteBroker a shape Kite doesn't send. Violations are recorded even when the
code under test swallows them; `verify()` fails on any.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

from executor.brokers.feeds.manual_feed import ManualFeed
from executor.brokers.kite_broker import KiteBroker
from executor.brokers.kite_contract import check_shape
from executor.brokers.kitesim import InstrumentBook, KiteSim, KiteSimConfig, SimFeed
from executor.models import BrokerName
from tests import kite_fixtures as kf
from tests.kite_sdk import METHOD_KINDS, ContractViolation

IST = ZoneInfo("Asia/Kolkata")
DAY_START = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
_ROWS = kf.sdk("instruments")


class Clock:
    def __init__(self, now: datetime = DAY_START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def fixture_instruments(synthesize_unknown: bool = True) -> InstrumentBook:
    """The documented instrument dump (INFY tick 0.10, KPEL 0.05, IDEA 0.01, ...);
    symbols it doesn't list get a 0.05-tick stand-in unless told otherwise."""
    return InstrumentBook(
        lambda exchange: [dict(r) for r in _ROWS if r["exchange"] == exchange],
        synthesize_unknown=synthesize_unknown,
    )


def make_sim(cash: float = 1_000_000.0, clock=None, **config) -> KiteSim:
    config.setdefault("user_id", "AB1234")
    return KiteSim(
        instruments=fixture_instruments(),
        cash=cash,
        clock=clock or Clock(),
        config=KiteSimConfig(**config),
    )


class CheckedKite:
    """`sim` as KiteBroker sees it, held to the SDK's signatures and Kite's shapes."""

    def __init__(self, sim: KiteSim) -> None:
        self.sim = sim
        self.calls: list[tuple[str, dict]] = []
        self.violations: list[str] = []

    def __getattr__(self, name: str):
        attr = getattr(self.sim, name)
        if not callable(attr) or not hasattr(KiteConnect, name) or name.isupper():
            return attr
        sig = inspect.signature(getattr(KiteConnect, name))

        def call(*args, **kwargs):
            bound = sig.bind(None, *args, **kwargs).arguments
            bound.pop("self", None)
            self.calls.append((name, dict(bound)))
            result = attr(*args, **kwargs)
            kind = METHOD_KINDS.get(name)
            if kind == "margins_equity" and bound.get("segment") != "equity":
                kind = None
            problems = check_shape(kind, result) if kind else []
            if problems:
                msg = f"KiteSim.{name}() returned a shape Kite never sends: {problems}"
                self.violations.append(msg)
                raise ContractViolation(msg)
            return result

        return call

    def called(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]

    def verify(self) -> None:
        if self.violations:
            raise ContractViolation("; ".join(self.violations))


def paper_broker(sim: KiteSim, feed: ManualFeed | None = None, checked: bool = True) -> KiteBroker:
    """KiteBroker on KiteSim, wired like paper mode: feed ticks drive the sim,
    sim postbacks arrive through the broker's on_order_update."""
    kite = CheckedKite(sim) if checked else sim
    return KiteBroker(
        feed=SimFeed(feed or ManualFeed(), sim), kite=kite, broker_name=BrokerName.PAPER
    )


def live_shaped_broker(sim: KiteSim, checked: bool = True) -> KiteBroker:
    """KiteBroker labelled KITE on KiteSim: stands in for the live broker in tests of
    pre-flight / canary / kite_checks, which only ever run on the real KiteBroker."""
    return KiteBroker(kite=CheckedKite(sim) if checked else sim)


class Script:
    """Scripted Kite misbehaviour for one test, through KiteSim's interceptor hook.

    Not hostile mode (#87): just the handful of outcomes the canary / pre-flight
    tests need, each produced the way Kite would show it."""

    def __init__(self, sim: KiteSim) -> None:
        self.sim = sim
        self.raises: dict[str, Exception] = {}  # call -> raise instead of answering
        self.reject: set[str] = set()  # transaction types RMS-rejected after placement
        self.fill: set[str] = set()  # transaction types filled at once at the LTP
        self.stuck: str | None = None  # in-flight status a new order never leaves
        self.ltp_script: list[float] = []  # successive ltp() prices; the last one sticks
        self.transform: dict[str, object] = {}  # call -> f(result) -> what Kite "sent"
        sim.interceptor = self

    def __call__(self, call: str, params: dict, proceed):
        if call in self.raises:
            raise self.raises[call]
        if call == "ltp" and self.ltp_script:
            price = self.ltp_script.pop(0) if len(self.ltp_script) > 1 else self.ltp_script[0]
            for key in _ltp_keys(params["instruments"]):
                exchange, _, symbol = key.partition(":")
                self.sim.on_price(symbol, exchange, price)
        result = proceed()
        if call == "place_order":
            side = params["transaction_type"]
            if side in self.reject:
                msg = "RMS: rejected"
                self.sim.advance(
                    result,
                    "REJECTED",
                    status_message=msg,
                    status_message_raw=msg,
                    pending_quantity=0,
                )
            elif side in self.fill:
                ltp = self.sim._ltp[f"{params['exchange']}:{params['tradingsymbol']}"]
                qty = params["quantity"]
                self.sim.advance(
                    result, "COMPLETE", filled_quantity=qty, pending_quantity=0, average_price=ltp
                )
            elif self.stuck:
                self.sim.advance(result, self.stuck)
        f = self.transform.get(call)
        return f(result) if f else result  # type: ignore[operator]


def _ltp_keys(instruments: tuple) -> list[str]:
    keys = list(instruments)
    return list(keys[0]) if keys and isinstance(keys[0], list) else keys


class SimKite:
    """KiteSim standing in for the real Kite account in pre-flight / canary tests:
    a priced INFY, the SDK call log (`sdk`), and scripted faults (`script`)."""

    def __init__(self, ltp: float = 1500.0, **config) -> None:
        self.sim = make_sim(**config)
        self.sim.on_price("INFY", "NSE", ltp)
        self.sdk = CheckedKite(self.sim)
        self.script = Script(self.sim)

    def broker(self, checked: bool = True) -> KiteBroker:
        return KiteBroker(kite=self.sdk if checked else self.sim)

    @property
    def book(self) -> dict[str, dict]:
        return {o["order_id"]: o for o in self.sim.orders()}

    def side(self, side: str) -> list[dict]:
        return [o for o in self.sim.orders() if o["transaction_type"] == side]
