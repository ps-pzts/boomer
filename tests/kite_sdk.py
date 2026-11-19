"""An autospec'd `kiteconnect.KiteConnect` that only returns what Kite can (#84).

    kite = fake_kite()                       # or the `kite_sdk` pytest fixture
    respond(kite, "orders", kf.sdk("orders"))
    broker._kite = kite

- Autospec: a wrong method name or argument fails like the real SDK would.
- Every response (`respond(...)` or a plain `.return_value = ...`) is checked
  against `executor.brokers.kite_contract` when the method is called. A shape
  Kite never produces raises ContractViolation and is recorded, so `verify()`
  (run by the fixture's teardown) fails the test even when the code under test
  swallowed the error (e.g. get_ltp's fallback chain).
- Exceptions must be ones the SDK raises: kiteconnect's own, or requests'.
- Calling a method the test gave no response is a violation, not a silent Mock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import NonCallableMock, create_autospec

import requests
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from executor.brokers.kite_contract import check_shape

# SDK method -> contract kind of its return value.
METHOD_KINDS: dict[str, str] = {
    "orders": "orders",
    "order_history": "orders",
    "positions": "positions",
    "holdings": "holdings",
    "margins": "margins_equity",  # only margins(segment="equity") is used
    "ltp": "ltp",
    "instruments": "instruments",
    "historical_data": "historical",
    "place_order": "place_order",
    "modify_order": "place_order",
    "cancel_order": "place_order",
    "get_gtt": "gtt",
    "get_gtts": "gtts",
    "place_gtt": "gtt_ack",
    "modify_gtt": "gtt_ack",
    "delete_gtt": "gtt_ack",
    "generate_session": "session",
}

_SDK_ERRORS = (KiteException, requests.RequestException)
_VIOLATIONS: dict[int, list[str]] = {}


class ContractViolation(AssertionError):
    """A test fed the code a Kite response Kite never produces."""


class _Checked:
    def __init__(self, kite: Any, name: str) -> None:
        self.kite, self.name, self.results = kite, name, []

    def _fail(self, message: str) -> ContractViolation:
        _VIOLATIONS.setdefault(id(self.kite), []).append(message)
        return ContractViolation(message)

    def _next(self) -> Any:
        if self.results:
            return self.results.pop(0) if len(self.results) > 1 else self.results[0]
        configured = getattr(self.kite, self.name).return_value
        if isinstance(configured, NonCallableMock):
            raise self._fail(f"kite.{self.name}() called but the test gave it no response")
        return configured

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        result = self._next()
        if isinstance(result, type) and issubclass(result, BaseException):
            result = result()
        if isinstance(result, BaseException):
            if not isinstance(result, _SDK_ERRORS):
                raise self._fail(
                    f"kite.{self.name}() raised {type(result).__name__}, which kiteconnect "
                    "never raises; use kite_fixtures.sdk_error(...)"
                )
            raise result
        if callable(result):
            result = result(*args, **kwargs)
        if self.name == "margins" and (kwargs.get("segment") or (args or [None])[0]) != "equity":
            raise self._fail("kite.margins() has a contract only for segment='equity'")
        problems = check_shape(METHOD_KINDS[self.name], result)
        if problems:
            raise self._fail(f"kite.{self.name}() returned a shape Kite never sends: {problems}")
        return result


def fake_kite() -> Any:
    """An autospec'd KiteConnect instance with the SDK's constants and contract checks."""
    kite = create_autospec(KiteConnect, instance=True)
    for name in dir(KiteConnect):
        if name.isupper():
            setattr(kite, name, getattr(KiteConnect, name))
    for name in METHOD_KINDS:
        getattr(kite, name).side_effect = _Checked(kite, name)
    _VIOLATIONS[id(kite)] = []
    return kite


def respond(kite: Any, method: str, *results: Any) -> None:
    """kite.<method>() returns / raises `results` in order; the last one repeats.

    A result may be a value, an exception, or a callable taking the call's args."""
    if not results:
        raise ValueError("respond() needs at least one result")
    _checker(kite, method).results = list(results)


def _checker(kite: Any, method: str) -> _Checked:
    effect = getattr(kite, method).side_effect
    if not isinstance(effect, _Checked):
        raise ContractViolation(
            f"kite.{method}.side_effect was replaced; use respond() so responses are checked"
        )
    return effect


def verify(kite: Any) -> None:
    """Fail if any response broke the contract (even if the code swallowed it), or
    a test bypassed the checks by setting side_effect directly."""
    for method in METHOD_KINDS:
        _checker(kite, method)
    violations = _VIOLATIONS.get(id(kite), [])
    if violations:
        raise ContractViolation("; ".join(violations))
