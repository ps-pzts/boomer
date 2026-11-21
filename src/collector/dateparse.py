"""
Strict date parsing shared by the collector parsers (#54).

A date that cannot be parsed is *unknown*, never "today": stamping a row with an
invented date writes data onto a day it was not published, which is a lookahead /
point-in-time bug for every feature built "as of" that day (CLAUDE.md §9).

Parsers call ``parse_date`` / ``parse_datetime`` (both return ``None`` on failure),
skip the row, and record the outcome in a ``ParseTally``. ``ParseTally.finish`` logs
the per-file failure count and raises ``DateFormatError`` when the failed fraction
exceeds ``MAX_FAILED_FRACTION`` — that pattern means the source changed its format,
not that a few rows are dirty. The ParseWorker marks the raw row FAILED on the raise.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# Above this fraction of date-bearing rows failing to parse, the file is treated as a
# format change and rejected whole. A percentage, not a count: files range from a few
# dozen filings to ~50k bhavcopy rows.
MAX_FAILED_FRACTION = 0.05


class DateFormatError(ValueError):
    """Too many rows in one file had unparseable dates — probable source format change."""


def parse_date(s: str | None, formats: tuple[str, ...]) -> str | None:
    """Return the ISO date for ``s`` using the first matching format, else None."""
    parsed = parse_datetime(s, formats)
    return parsed.date().isoformat() if parsed else None


def parse_datetime(s: str | None, formats: tuple[str, ...]) -> datetime | None:
    """Return the datetime for ``s`` using the first matching format, else None."""
    text = (s or "").strip()
    if not text:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass
class ParseTally:
    """Counts date-bearing rows seen vs rows skipped for an unparseable date."""

    label: str
    total: int = 0
    failed: int = 0

    def ok(self) -> None:
        self.total += 1

    def fail(self, sample: str | None = None) -> None:
        self.total += 1
        self.failed += 1
        if self.failed <= 3:  # a few samples are enough to diagnose a format change
            logger.warning("%s: unparseable date %r — row skipped", self.label, sample)

    @property
    def fraction(self) -> float:
        return self.failed / self.total if self.total else 0.0

    def finish(self, raw_id: str | None = None, db: sqlite3.Connection | None = None) -> None:
        """
        Log the per-file count; raise DateFormatError above MAX_FAILED_FRACTION.

        When ``db`` is given, the uncommitted inserts are rolled back before raising so
        a rejected file leaves no partial rows (a re-parse after a parser fix is clean).
        """
        if not self.failed:
            return
        logger.warning(
            "%s: %d of %d rows skipped for unparseable dates (%.1f%%) raw_id=%s",
            self.label,
            self.failed,
            self.total,
            100 * self.fraction,
            raw_id,
        )
        if self.fraction > MAX_FAILED_FRACTION:
            if db is not None:
                db.rollback()
            raise DateFormatError(
                f"{self.label}: {self.failed}/{self.total} rows have unparseable dates "
                f"(> {MAX_FAILED_FRACTION:.0%}) — likely a source format change"
            )
