"""Enforce the one-way event stream (designs/observability.md §2, §11; #56).

Operator rule: every part of the system WRITES events, only the observability
module READS them, and no trading code ever reads events to make a decision.
This test fails the build if anything outside src/observability/ queries the
`events` table or imports an observability reader.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "src"
ALLOWED_DIR = SRC / "observability"

# SQL that reads events: FROM events / JOIN events (not "INSERT INTO events").
_READ_SQL = re.compile(r"\b(from|join)\s+events\b", re.IGNORECASE)
# Anything in observability other than the emitter is reader-side.
_READER_IMPORT = re.compile(
    r"^\s*(from|import)\s+observability(\.(?!emit\b)\w+|\s+import\s+(?!emit\b)\w+)",
    re.MULTILINE,
)


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    hits = []
    for path in SRC.rglob("*.py"):
        if ALLOWED_DIR in path.parents:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(SRC.parent)}:{lineno}: {line.strip()}")
    return hits


def test_no_trading_code_reads_the_events_table():
    assert _offenders(_READ_SQL) == [], (
        "Only src/observability/ may read the events table. Trading decisions must "
        "use each machine's own state table, never events."
    )


def test_no_trading_code_imports_observability_readers():
    assert _offenders(_READER_IMPORT) == [], "Only `observability.emit` may be imported."


def test_patterns_catch_what_they_should():
    """Guard the guard: the regexes really match reads and really allow writes."""
    assert _READ_SQL.search('conn.execute("SELECT * FROM events WHERE kind=?")')
    assert _READ_SQL.search("select 1 from x join events e on e.id = x.id")
    assert not _READ_SQL.search("INSERT INTO events (ts) VALUES (?)")
    assert not _READ_SQL.search("SELECT * FROM events_archive")
    assert _READER_IMPORT.search("from observability.projection import Live")
    assert _READER_IMPORT.search("from observability import projection")
    assert not _READER_IMPORT.search("from observability import emit as obs")
    assert not _READER_IMPORT.search("from observability.emit import emit")
