"""
NSE surveillance lists: ASM (Additional Surveillance Measure) and GSM (Graded
Surveillance Measure) — #50 part 2. Stocks on either list are excluded from the
intraday universe by brain/universe.py.

UNVERIFIED AGAINST THE LIVE SITE. When this was written the NSE endpoints could not
be reached from the build environment, so neither the JSON schema of
`/api/reportASM` / `/api/reportGSM` nor the CSV-archive fallback URLs below have been
confirmed. The parser therefore does not assume a schema: it walks the JSON for any
object carrying a `symbol` key (or reads a CSV `Symbol` column), and the fetch is
rejected, never recorded as an empty list, when no symbols are found. Verify the
endpoints (see the issue #50 thread) before trusting this in production; until then
the staleness policy in universe.py keeps the system F&O-only with a CRITICAL alert.

Access: NSE's API needs a browser-like session. We GET the HTML page first so the
session picks up its cookies, then call the API with JSON headers and the page as
Referer. If the API fails, a CSV archive URL is tried.

list_date: NSE publishes the current list only, so a list is valid from the day it is
fetched. parse() stamps the IST date of the fetch, not the collector's trade date.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from collector.base import BaseFetcher, PermanentFetchError, _fmt_dt, _now_ist
from collector.models import DataSource, FetchResult, ParseStatus, RawArchiveRow

logger = logging.getLogger(__name__)

LIST_TYPES = ("ASM", "GSM")

_HOME = "https://www.nseindia.com/"
_PAGES = {
    "ASM": "https://www.nseindia.com/reports/asm",
    "GSM": "https://www.nseindia.com/reports/gsm",
}
_API = {
    "ASM": "https://www.nseindia.com/api/reportASM",
    "GSM": "https://www.nseindia.com/api/reportGSM",
}
# UNVERIFIED placeholders for a published CSV; a 404 is tolerated and skipped.
_CSV_FALLBACK = {
    "ASM": "https://nsearchives.nseindia.com/content/cm/ASM_List.csv",
    "GSM": "https://nsearchives.nseindia.com/content/cm/GSM_List.csv",
}

Row = tuple[str, "str | None", "str | None"]  # (symbol, isin, stage)


class SurveillanceFetcher(BaseFetcher):
    source = DataSource.SURVEILLANCE

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update({"Referer": _HOME, "Accept-Language": "en-US,en;q=0.9"})

    # ── fetch ─────────────────────────────────────────────────────────────

    def run(self, **kwargs) -> RawArchiveRow | None:
        """Fetch ASM and GSM (API first, CSV fallback). Returns the last row archived.

        No long backoff here: the scheduler retries the whole nightly task, and the
        universe's staleness policy covers a list that stays unavailable.
        """
        last: RawArchiveRow | None = None
        for list_type in LIST_TYPES:
            row = self._fetch_list(list_type)
            if row is None:
                logger.error("surveillance %s list unavailable from every source", list_type)
            else:
                last = row
        return last

    def refresh(self, list_types: tuple[str, ...] = LIST_TYPES) -> dict[str, int]:
        """Fetch and parse straight away (the mid-session retry); symbols stored per type."""
        stored: dict[str, int] = {}
        for list_type in list_types:
            row = self._fetch_list(list_type)
            if row is not None:
                stored[list_type] = self.parse(row)
        return stored

    def _fetch_list(self, list_type: str) -> RawArchiveRow | None:
        for via, url in (("api", _API[list_type]), ("csv", _CSV_FALLBACK[list_type])):
            try:
                result = self._get(url, list_type, via)
                self.validate(result)
                return self.archive(result)
            except PermanentFetchError as exc:
                logger.info("surveillance %s via %s skipped: %s", list_type, via, exc)
            except Exception as exc:
                logger.warning("surveillance %s via %s failed: %s", list_type, via, exc)
        return None

    def _get(self, url: str, list_type: str, via: str) -> FetchResult:
        headers: dict[str, str] = {}
        if via == "api":
            # Cookie handshake: the API rejects a session that has not loaded a page.
            self._session.headers.update({"User-Agent": self._next_ua()})
            self._session.get(_HOME, timeout=20)
            self._session.get(_PAGES[list_type], timeout=20)
            headers = {"Accept": "application/json, text/plain, */*", "Referer": _PAGES[list_type]}
        resp = self._session.get(url, headers=headers, timeout=30)
        fetched_at = _now_ist()
        body = resp.content
        # The hash includes the fetch date: archive() dedups on content_hash, and an
        # unchanged list on a later day must still be archived (and parsed) so its
        # list_date advances.
        digest = hashlib.sha256(body + fetched_at.date().isoformat().encode()).hexdigest()
        return FetchResult(
            source=self.source,
            url=resp.url,
            status_code=resp.status_code,
            body=body,
            content_hash=digest,
            fetched_at=fetched_at,
            params={"list_type": list_type, "via": via},
        )

    def fetch_url(self, **kwargs) -> str:  # required by the ABC; run() is overridden
        return _API["ASM"]

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise PermanentFetchError(f"surveillance: 404 for {result.url}")
        if result.status_code != 200:
            raise ValueError(f"surveillance: HTTP {result.status_code} for {result.url}")
        # Never accept a response that yields no symbols: an empty or reshaped payload
        # must not be recorded as "the list is empty" (that would un-exclude every stock).
        if not extract_rows(result.body):
            raise ValueError(f"surveillance: no symbols found in response from {result.url}")

    # ── parse ─────────────────────────────────────────────────────────────

    def parse(self, raw_row: RawArchiveRow) -> int:
        params = json.loads(raw_row.request_params or "{}")
        list_type = params.get("list_type")
        if list_type not in LIST_TYPES:
            raise ValueError(f"surveillance parse: unknown list_type in {raw_row.request_params}")
        rows = extract_rows(self.load_raw_body(raw_row.content_path))
        if not rows:
            raise ValueError("surveillance parse: no symbols")  # parse FAILED, not an empty list
        list_date = raw_row.fetched_at.date().isoformat()  # IST: _now_ist() is IST-naive
        observed = _fmt_dt(raw_row.fetched_at)
        for symbol, isin, stage in rows:
            self._db.execute(
                "INSERT OR REPLACE INTO surveillance_list"
                " (list_date, list_type, symbol, isin, stage, raw_id, observed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (list_date, list_type, symbol, isin, stage, raw_row.raw_id, observed),
            )
        self._db.commit()
        self.mark_parsed(raw_row.raw_id, self.parser_version, ParseStatus.SUCCESS)
        return len(rows)


# ── Payload parsing (schema-agnostic) ───────────────────────────────────────────


def extract_rows(body: bytes) -> list[Row]:
    """(symbol, isin, stage) per distinct symbol, from a JSON or CSV payload."""
    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    found: list[Row] = []
    try:
        _walk(json.loads(text), found, None)
    except ValueError:
        found = _from_csv(text)
    seen: dict[str, Row] = {}
    for row in found:
        seen.setdefault(row[0], row)
    return list(seen.values())


def _clean(value: Any) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s or None


def _walk(node: Any, out: list[Row], hint: str | None) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, out, hint)
    elif isinstance(node, dict):
        lowered = {str(k).lower(): v for k, v in node.items()}
        symbol = _clean(lowered.get("symbol"))
        if symbol:
            stage = next(
                (
                    _clean(v)
                    for k, v in lowered.items()
                    if ("stage" in k or k in ("survcode", "asmsurvindicator")) and _clean(v)
                ),
                hint,
            )
            out.append((symbol.upper(), _clean(lowered.get("isin")), stage))
        else:
            for key, value in node.items():
                generic = str(key).lower() in ("data", "rows", "records", "result", "results")
                _walk(value, out, hint if generic else str(key))


def _from_csv(text: str) -> list[Row]:
    out: list[Row] = []
    for row in csv.DictReader(io.StringIO(text)):
        lowered = {(k or "").strip().lower(): v for k, v in row.items()}
        symbol = _clean(lowered.get("symbol"))
        if not symbol:
            continue
        isin = _clean(lowered.get("isin") or lowered.get("isin code"))
        stage = next((_clean(v) for k, v in lowered.items() if "stage" in k and _clean(v)), None)
        out.append((symbol.upper(), isin, stage))
    return out
