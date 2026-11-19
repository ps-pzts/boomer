"""Daily Kite pre-flight (#85): read-only checks of the real Kite plumbing.

Runs at 08:55 IST every trading day, paper days included: paper trading never
exercises the live Kite path, and Kite can change behaviour under us. Every
check is read-only; nothing is placed. The session may not reach TRADING until
today's pre-flight (and the 09:15 canary, `executor/canary.py`) have passed.

Checks, in order (a dead token skips the other broker checks):
  token        profile() succeeds                         (access token valid)
  funds        get_funds() + raw margins shape
  instruments  the probe instrument is in the dump with a tick size
  orders       raw orders() shape for every order + list_open_orders()
  positions    raw positions() shape + list_positions()
  ltp          raw ltp() shape for the probe instrument
  feed         the market-data feed is connected (skipped when this process has none)
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from executor.brokers.kite_contract import check_shape

IST = ZoneInfo("Asia/Kolkata")
FEED_WAIT_POLLS = 10  # x 1 s: the feed connected at 08:45, this only absorbs a reconnect


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class CheckReport:
    kind: str  # "preflight" | "canary"
    results: tuple[CheckResult, ...]
    ran_at: str  # IST wall clock, ISO
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    def failures(self) -> list[str]:
        return [f"{r.name}: {r.detail}" for r in self.results if not r.ok]

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "ran_at": self.ran_at,
            "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in self.results],
            **self.extra,
        }


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def run_preflight(
    broker: object,
    feed: object | None = None,
    *,
    symbol: str = "INFY",
    exchange: str = "NSE",
    clock: Callable[[], datetime] = lambda: datetime.now(IST),
    sleep: Callable[[float], None] = _time.sleep,
) -> CheckReport:
    """Run every check against `broker` (a KiteBroker) and `feed`; never raises."""
    b = broker
    results = [_check("token", lambda: _token(b))]
    broker_checks: list[tuple[str, Callable[[], str]]] = [
        ("funds", lambda: _funds(b)),
        ("instruments", lambda: _instrument(b, symbol, exchange)),
        ("orders", lambda: _orders(b)),
        ("positions", lambda: _positions(b)),
        ("ltp", lambda: _ltp(b, f"{exchange}:{symbol}")),
    ]
    for name, fn in broker_checks:
        if results[0].ok:
            results.append(_check(name, fn))
        else:
            results.append(CheckResult(name, False, "not run: token invalid"))
    results.append(_feed(feed, sleep))
    return CheckReport("preflight", tuple(results), clock().replace(tzinfo=None).isoformat())


def _check(name: str, fn: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(name, True, fn())
    except Exception as exc:
        return CheckResult(name, False, _err(exc))


class ShapeError(Exception):
    pass


def _shape(kind: str, payload: object) -> None:
    problems = check_shape(kind, payload)
    if problems:
        raise ShapeError("; ".join(problems))


def _token(b) -> str:
    profile = b.raw_read("profile")
    user = (profile or {}).get("user_id") if isinstance(profile, dict) else None
    if not user:
        raise ShapeError("profile has no user_id")
    return str(user)


def _funds(b) -> str:
    _shape("margins_equity", b.raw_read("margins_equity"))
    funds = b.get_funds()
    return f"available_cash={float(funds.available_cash):.2f}"


def _instrument(b, symbol: str, exchange: str) -> str:
    row = b.instruments.row(symbol, exchange)
    if row is None:
        raise LookupError(f"{exchange}:{symbol} not in the instrument dump")
    _shape("instrument", row)
    tick = float(row["tick_size"])
    if tick <= 0:
        raise ShapeError(f"{exchange}:{symbol} tick_size={tick}")
    return f"{exchange}:{symbol} tick={tick}"


def _orders(b) -> str:
    raw = b.raw_read("orders")
    if not isinstance(raw, list):
        raise ShapeError(f"orders: expected list, got {type(raw).__name__}")
    for o in raw:
        _shape("order", o)
    working = b.list_open_orders()
    return f"orders={len(raw)} working={len(working)}"


def _positions(b) -> str:
    _shape("positions", b.raw_read("positions"))
    return f"mis_positions={len(b.list_positions())}"


def _ltp(b, key: str) -> str:
    raw = b.raw_read("ltp", key)
    _shape("ltp", raw)
    if key not in raw:
        raise ShapeError(f"ltp: {key} missing from the response")
    return f"{key}={raw[key]['last_price']}"


def _feed(feed: object | None, sleep: Callable[[float], None]) -> CheckResult:
    if feed is None:
        return CheckResult("feed", True, "skipped: no feed in this process")
    try:
        for i in range(FEED_WAIT_POLLS):
            health = feed.health()  # type: ignore[attr-defined]
            if health.connected:
                return CheckResult("feed", True, f"connected subscribed={health.subscribed}")
            if i < FEED_WAIT_POLLS - 1:
                sleep(1.0)
        return CheckResult("feed", False, "feed not connected")
    except Exception as exc:
        return CheckResult("feed", False, _err(exc))
