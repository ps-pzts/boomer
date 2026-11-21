"""capital/sizing.py — shrink to the single-stock cap (#69). Numbers from the module doc."""

from decimal import Decimal

import pytest

from capital.sizing import intraday_quantity

D = Decimal


def _size(entry="1000", stop="970", capital="1000000", scale="1.00"):
    return intraday_quantity(
        bucket_capital=D(capital),
        total_capital=D(capital),
        risk_pct=D("0.005"),
        single_stock_cap_pct=D("0.05"),
        regime_scale=D(scale),
        entry=D(entry),
        stop=D(stop),
    )


def test_worked_example_cap_binds():
    s = _size()
    assert (s.risk_shares, s.cap_shares, s.quantity, s.binding) == (166, 50, 50, "single_stock_cap")
    assert s.risk_rupees == D("1500")


def test_risk_binds_when_stop_is_wide():
    # stop 100 away: 5,000 / 100 = 50 risk shares; cap 50 -> equal, risk binds (not smaller)
    s = _size(stop="900")
    assert (s.quantity, s.binding) == (50, "risk")
    s = _size(stop="850")  # 5,000 / 150 = 33
    assert (s.quantity, s.binding, s.risk_rupees) == (33, "risk", D("4950"))


def test_regime_scale_applies_to_risk_size():
    s = _size(stop="850", scale="0.50")  # 33 × 0.5 = 16
    assert s.quantity == 16


def test_too_small_is_zero():
    assert _size(entry="60000", stop="59000").quantity == 0  # cap: 50,000 / 60,000 = 0


@pytest.mark.parametrize("stop", ["1000", "1100", "0"])
def test_stop_must_be_below_entry(stop):
    with pytest.raises(ValueError):
        _size(stop=stop)


def test_low_priced_stock_is_valid():
    # entry 1, stop 0.50 -> 0.50 a share: 5,000 / 0.50 = 10,000 risk; cap 50,000 / 1 = 50,000
    s = _size(entry="1", stop="0.5")
    assert (s.quantity, s.binding, s.risk_rupees) == (10000, "risk", D("5000.0"))
    # entry 2, stop 1 -> 1 a share: 5,000 risk shares; cap 50,000 / 2 = 25,000
    s = _size(entry="2", stop="1")
    assert (s.quantity, s.binding, s.risk_rupees) == (5000, "risk", D("5000"))


def test_sizing_is_immutable():
    with pytest.raises(AttributeError):
        _size().quantity = 1  # type: ignore[misc]


def test_non_positive_entry_is_refused():
    # entry <= 0 is already caught by the stop checks, so the entry guard is redundant
    # (mutant `entry < 0` is equivalent); this pins the behaviour.
    for entry, stop in [("0", "-1"), ("-5", "-10")]:
        with pytest.raises(ValueError):
            _size(entry=entry, stop=stop)
