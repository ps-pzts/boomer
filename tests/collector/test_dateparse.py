"""Tests for strict collector date parsing (#54)."""

import logging
import sqlite3

import pytest

from collector.dateparse import (
    MAX_FAILED_FRACTION,
    DateFormatError,
    ParseTally,
    parse_date,
    parse_datetime,
)

FMTS = ("%d-%b-%Y", "%Y-%m-%d")


def test_parse_date_matches_known_formats():
    assert parse_date("25-Apr-2024", FMTS) == "2024-04-25"
    assert parse_date(" 2024-04-25 ", FMTS) == "2024-04-25"


@pytest.mark.parametrize("bad", ["", "   ", None, "garbage", "31-02-2024", "25/04/2024"])
def test_parse_date_returns_none_never_today(bad):
    assert parse_date(bad, FMTS) is None


def test_parse_datetime_returns_datetime():
    dt = parse_datetime("2024-04-25", FMTS)
    assert dt is not None and dt.year == 2024


def test_tally_below_threshold_logs_and_passes(caplog):
    t = ParseTally("X")
    for _ in range(99):
        t.ok()
    t.fail("bad")  # 1 / 100 = 1% <= 5%
    with caplog.at_level(logging.WARNING):
        t.finish("raw-1")
    assert "1 of 100" in caplog.text


def test_tally_no_failures_is_silent(caplog):
    t = ParseTally("X")
    t.ok()
    with caplog.at_level(logging.WARNING):
        t.finish("raw-1")
    assert caplog.text == ""


def test_tally_empty_file_does_not_raise():
    ParseTally("X").finish("raw-1")


def test_tally_above_threshold_raises():
    t = ParseTally("X")
    for _ in range(9):
        t.ok()
    t.fail("bad")  # 10% > 5%
    with pytest.raises(DateFormatError, match="format change"):
        t.finish("raw-1")


def test_tally_whole_file_failing_raises():
    t = ParseTally("X")
    for _ in range(3):
        t.fail("bad")
    with pytest.raises(DateFormatError):
        t.finish("raw-1")


def test_tally_exactly_at_threshold_passes():
    t = ParseTally("X")
    n = round(1 / MAX_FAILED_FRACTION)  # 20 rows -> 1 failure == 5.0%
    for _ in range(n - 1):
        t.ok()
    t.fail("bad")
    t.finish("raw-1")


def test_tally_rolls_back_uncommitted_rows_on_raise():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.execute("INSERT INTO t VALUES (1)")
    t = ParseTally("X")
    t.fail("bad")
    with pytest.raises(DateFormatError):
        t.finish("raw-1", db)
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
