"""Event emitter: the only way anything writes to the `events` table (#56).

designs/observability.md §2, §4-5, §7. Two write paths share one envelope:

1. `emit()`: asynchronous, best-effort. Appends to an in-memory queue and
   returns at once; it NEVER raises and never blocks the caller. One writer
   thread per process, with its own SQLite connection (so the feed thread can
   emit), batch-inserts about once a second. A full queue drops the event and
   counts it; the count rides on the next heartbeat, and the `source_seq` gap
   shows it as well.
2. `envelope()` + `insert()`: synchronous, used by the state-machine core inside
   the same transaction as the state change, so a transition and its event
   commit together or not at all.

`source_seq` is monotonic per (source, boot_id) across both paths, so the
observability module can detect lost events.

Nothing here reads `events`. Only src/observability/ reads, never trading code.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

SCHEMA_VERSION = 1
QUEUE_MAX = 10_000
FLUSH_INTERVAL_SECONDS = 1.0
HEARTBEAT_SECONDS = 30.0
_DENYLIST = ("token", "secret", "password", "api_key", "apikey")
_COLUMNS = (
    "ts",
    "session_date",
    "mode",
    "source",
    "boot_id",
    "source_seq",
    "kind",
    "name",
    "severity",
    "entity_type",
    "entity_id",
    "cycle_id",
    "trade_id",
    "order_id",
    "symbol",
    "payload",
    "schema_version",
)
_INSERT = f"INSERT INTO events ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for k, v in payload.items():
        if any(word in k.lower() for word in _DENYLIST):
            logger.warning("event_payload_redacted key=%s", k)
            clean[k] = "[redacted]"
        else:
            clean[k] = v
    return clean


class Emitter:
    def __init__(
        self,
        db_path: str | None,
        source: str,
        mode: str = "unknown",
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
    ) -> None:
        self.db_path = db_path
        self.source = source
        self.mode = mode
        self.boot_id = uuid.uuid4().hex[:12]
        self._clock = clock
        self._seq: dict[str, int] = defaultdict(int)
        self._seq_lock = threading.Lock()
        self._queue: queue.Queue[tuple] = queue.Queue(maxsize=QUEUE_MAX)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.dropped = 0
        self.written = 0
        self.write_errors = 0

    # ── Envelope (shared by both paths) ───────────────────────────────────────

    def envelope(
        self,
        kind: str,
        name: str,
        *,
        source: str | None = None,
        severity: str = "info",
        entity_type: str | None = None,
        entity_id: str | None = None,
        cycle_id: str | None = None,
        trade_id: str | None = None,
        order_id: str | None = None,
        symbol: str | None = None,
        **payload: Any,
    ) -> tuple:
        src = source or self.source
        with self._seq_lock:
            self._seq[src] += 1
            seq = self._seq[src]
        now = self._clock()
        return (
            now.isoformat(timespec="milliseconds"),
            now.date().isoformat(),
            self.mode,
            src,
            self.boot_id,
            seq,
            kind,
            name,
            severity,
            entity_type,
            entity_id,
            cycle_id,
            trade_id,
            order_id,
            symbol,
            json.dumps(_redact(payload), default=str, sort_keys=True),
            SCHEMA_VERSION,
        )

    @staticmethod
    def insert(conn: sqlite3.Connection, row: tuple) -> None:
        """Synchronous insert on the caller's connection (and transaction)."""
        conn.execute(_INSERT, row)

    # ── Async path ────────────────────────────────────────────────────────────

    def emit(self, kind: str, name: str, **fields: Any) -> None:
        """Queue an event. Never raises, never blocks."""
        try:
            row = self.envelope(kind, name, **fields)
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1
        except Exception:  # an emit bug must never reach trading code
            self.dropped += 1
            logger.exception("emit_failed kind=%s name=%s", kind, name)

    def start(
        self,
        heartbeat_seconds: float | None = -1.0,  # -1: use module HEARTBEAT_SECONDS
        health: Callable[[], dict] | None = None,
    ) -> None:
        if self.db_path is None:
            raise RuntimeError("Emitter without db_path can't start a writer")
        writer = threading.Thread(target=self._writer, name=f"events-{self.source}", daemon=True)
        self._threads.append(writer)
        writer.start()
        self.emit("lifecycle", "started", entity_type="component", entity_id=self.source)
        if heartbeat_seconds == -1.0:
            heartbeat_seconds = HEARTBEAT_SECONDS
        if heartbeat_seconds:
            beat = threading.Thread(
                target=self._heartbeat,
                args=(heartbeat_seconds, health),
                daemon=True,
                name=f"heartbeat-{self.source}",
            )
            self._threads.append(beat)
            beat.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.emit("lifecycle", "stopped", entity_type="component", entity_id=self.source)
        self._stop.set()
        for t in self._threads:
            t.join(timeout)

    def stats(self) -> dict[str, int]:
        return {
            "queue_depth": self._queue.qsize(),
            "dropped": self.dropped,
            "written": self.written,
            "write_errors": self.write_errors,
        }

    def _writer(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            while not (self._stop.is_set() and self._queue.empty()):
                batch = self._drain()
                if batch:
                    self._write(conn, batch)
        finally:
            conn.close()

    def _drain(self) -> list[tuple]:
        try:
            batch = [self._queue.get(timeout=FLUSH_INTERVAL_SECONDS)]
        except queue.Empty:
            return []
        while len(batch) < 500:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _write(self, conn: sqlite3.Connection, batch: list[tuple]) -> None:
        try:
            with conn:
                conn.executemany(_INSERT, batch)
            self.written += len(batch)
        except Exception:
            self.write_errors += 1
            self.dropped += len(batch)
            logger.exception("events_write_failed rows=%d", len(batch))

    def _heartbeat(self, interval: float, health: Callable[[], dict] | None) -> None:
        while not self._stop.wait(interval):
            extra: dict[str, Any] = {}
            if health is not None:
                try:
                    extra = health()
                except Exception as exc:
                    extra = {"health_error": str(exc)}
            self.emit(
                "heartbeat",
                "alive",
                entity_type="component",
                entity_id=self.source,
                **self.stats(),
                **extra,
            )


# ── Process-wide emitter ──────────────────────────────────────────────────────
# Until configure() is called (tests, scripts), async emit() is a counted no-op,
# but envelope() still works so state transitions are always recorded.
_current = Emitter(db_path=None, source="unconfigured")


def configure(db_path: str, source: str, mode: str, **start_kwargs: Any) -> Emitter:
    global _current
    _current = Emitter(db_path=db_path, source=source, mode=mode)
    _current.start(**start_kwargs)
    return _current


def get() -> Emitter:
    return _current


def emit(kind: str, name: str, **fields: Any) -> None:
    """Module-level convenience: `from observability.emit import emit`."""
    _current.emit(kind, name, **fields)
