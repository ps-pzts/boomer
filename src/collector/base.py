"""
BaseFetcher: five-method anatomy applied uniformly to every data source.

Each subclass overrides:
  fetch_url()   → URL to call (may be date-parameterized)
  transport()   → HTTP call; returns FetchResult
  validate()    → raises ValueError if response is malformed
  archive()     → writes Layer 1 raw_archive row + gzipped payload to disk
  parse()       → produces Layer 2 records (called separately from collection)
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

import requests

from collector.models import DataSource, FetchResult, ParseStatus, RawArchiveRow

logger = logging.getLogger(__name__)


class PermanentFetchError(Exception):
    """Raised when a fetch should not be retried — e.g. 404 on a date-stamped file
    that doesn't exist because the market was closed today."""


# Exponential backoff delays in seconds: attempt 0→1, 1→2, 2→3, 3→4, 4→alert+stop
_BACKOFF = [30, 60, 300, 1800]

_DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


class BaseFetcher(ABC):
    """
    Abstract base for all data-source fetchers.

    Subclasses must set `source` as a class attribute and implement
    fetch_url, validate, parse. Override transport or archive only when
    a source has unusual requirements (e.g. NSE session-cookie dance).
    """

    source: DataSource
    parser_version: str = "v1"
    _ua_index: int = 0

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        self._db = db
        self._raw_dir = raw_dir
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self._next_ua()})

    # ── public entry point ────────────────────────────────────────────────

    def run(self, **kwargs) -> RawArchiveRow | None:
        """
        Full collection cycle for one fetch. Handles backoff and error isolation.
        Returns the RawArchiveRow written, or None if all retries exhausted.

        Raises PermanentFetchError immediately (no retries) — used for 404s on
        date-stamped files where the file simply doesn't exist (weekend/holiday).
        """
        url = self.fetch_url(**kwargs)
        for attempt in range(len(_BACKOFF) + 1):
            try:
                result = self.transport(url, **kwargs)
                self.validate(result)
                return self.archive(result)
            except PermanentFetchError as exc:
                logger.info("fetch skipped source=%s: %s", self.source, exc)
                return None
            except Exception as exc:
                if attempt < len(_BACKOFF):
                    delay = _BACKOFF[attempt]
                    logger.warning(
                        "fetch failed source=%s attempt=%d, retrying in %ds: %s",
                        self.source,
                        attempt,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                else:
                    logger.error("fetch exhausted retries source=%s: %s", self.source, exc)
        return None

    # ── abstract methods ──────────────────────────────────────────────────

    @abstractmethod
    def fetch_url(self, **kwargs) -> str:
        """Return the URL to request."""

    @abstractmethod
    def validate(self, result: FetchResult) -> None:
        """Raise ValueError if the response is not well-formed."""

    @abstractmethod
    def parse(self, raw_row: RawArchiveRow) -> int:
        """
        Read raw payload from Layer 1 and write Layer 2 records.
        Returns count of new rows written. Called separately from collection.
        """

    # ── default transport — override for sources needing cookies/auth ─────

    def transport(self, url: str, **kwargs) -> FetchResult:
        timeout = kwargs.pop("timeout", 30)
        kwargs.pop("trade_date", None)  # consumed by fetch_url, not a URL param
        params = kwargs or None
        self._session.headers.update({"User-Agent": self._next_ua()})
        resp = self._session.get(url, params=params, timeout=timeout)
        # Don't raise_for_status here — let validate() decide whether 4xx is
        # permanent (PermanentFetchError) or retryable (ValueError).
        body = resp.content
        return FetchResult(
            source=self.source,
            url=resp.url,
            status_code=resp.status_code,
            body=body,
            content_hash=hashlib.sha256(body).hexdigest(),
            fetched_at=_now_utc(),
            params=params,
        )

    # ── default archive — writes Layer 1 row + gzipped payload ───────────

    def archive(self, result: FetchResult) -> RawArchiveRow:
        # Idempotent: same content_hash → return existing row without re-archiving.
        existing = self._db.execute(
            "SELECT raw_id, source, fetched_at, request_url, request_params, "
            "response_status, content_hash, content_path, parser_version, "
            "parsed_at, parse_status FROM raw_archive WHERE content_hash = ?",
            (result.content_hash,),
        ).fetchone()
        if existing:
            logger.debug("archive: dedup hit content_hash=%s", result.content_hash)
            return _row_from_db(existing)

        raw_id = str(uuid.uuid4())
        rel_path = self._content_path(result, raw_id)
        abs_path = self._raw_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(gzip.compress(result.body))

        row = RawArchiveRow(
            raw_id=raw_id,
            source=result.source,
            fetched_at=result.fetched_at,
            request_url=result.url,
            request_params=json.dumps(result.params, default=str) if result.params else None,
            response_status=result.status_code,
            content_hash=result.content_hash,
            content_path=str(rel_path),
        )
        self._db.execute(
            """
            INSERT INTO raw_archive
                (raw_id, source, fetched_at, request_url, request_params,
                 response_status, content_hash, content_path,
                 parser_version, parsed_at, parse_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.raw_id,
                row.source.value,
                _fmt_dt(row.fetched_at),
                row.request_url,
                row.request_params,
                row.response_status,
                row.content_hash,
                row.content_path,
                row.parser_version,
                None,
                row.parse_status.value,
            ),
        )
        self._db.commit()
        return row

    def mark_parsed(self, raw_id: str, version: str, status: ParseStatus) -> None:
        self._db.execute(
            "UPDATE raw_archive SET parser_version=?, parsed_at=?, parse_status=? WHERE raw_id=?",
            (version, _fmt_dt(_now_utc()), status.value, raw_id),
        )
        self._db.commit()

    def load_raw_body(self, content_path: str) -> bytes:
        return gzip.decompress((self._raw_dir / content_path).read_bytes())

    # ── helpers ───────────────────────────────────────────────────────────

    def _content_path(self, result: FetchResult, raw_id: str) -> Path:
        date_str = result.fetched_at.strftime("%Y/%m/%d")
        return Path(result.source.value) / date_str / f"{raw_id}.gz"

    @classmethod
    def _next_ua(cls) -> str:
        ua = _DEFAULT_USER_AGENTS[cls._ua_index % len(_DEFAULT_USER_AGENTS)]
        cls._ua_index += 1
        return ua


# ── module-level helpers ───────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _row_from_db(row: tuple) -> RawArchiveRow:
    raw_id, source, fetched_at, url, params, status, chash, cpath, pver, pat, pstatus = row
    return RawArchiveRow(
        raw_id=raw_id,
        source=DataSource(source),
        fetched_at=datetime.fromisoformat(fetched_at.rstrip("Z")),
        request_url=url,
        request_params=params,
        response_status=status,
        content_hash=chash,
        content_path=cpath,
        parser_version=pver,
        parsed_at=datetime.fromisoformat(pat.rstrip("Z")) if pat else None,
        parse_status=ParseStatus(pstatus),
    )
