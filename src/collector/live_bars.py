"""LiveBarStore: 1-minute OHLCV bars built from live ticks, persisted to parquet.

Design: designs/intraday-live-pipeline.md §3.2. Kite Personal has no historical
candles (§1b), so these bars are the only intraday history the system will ever
have. They are recorded from the first day the feed runs, whether or not the
pipeline trades.

- Only ticks inside the continuous session (09:15:00-15:29:59 IST) become bars.
  Pre-open ticks (09:00-09:08 equilibrium) are ignored.
- Bars are keyed by the IST minute the tick was *received* (Kite ticks carry no
  per-trade time in QUOTE mode, and we want one consistent clock).
- volume = change in the exchange's cumulative day volume across the bar. The
  first bar a symbol is seen in can only count volume from its first tick.
- flush() writes completed, not-yet-written bars to
  <root>/date=YYYY-MM-DD/part-HHMM.parquet, so a mid-day crash loses at most
  one flush interval (loophole L7).

Thread-safety: on_tick() runs on the feed thread; everything else on the
orchestrator/cycle threads. All state is behind one lock.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time
from pathlib import Path

from executor.brokers.feeds.base import Tick

logger = logging.getLogger(__name__)

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)  # exclusive: last bar is 15:29


@dataclass(frozen=True)
class MinuteBar:
    symbol: str
    exchange: str
    minute: datetime  # IST, second=0
    open: float
    high: float
    low: float
    close: float
    volume: int
    cum_volume: int | None  # exchange cumulative day volume at the bar's last tick
    vwap: float | None  # exchange day VWAP at the bar's last tick
    ticks: int


def _floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def in_session(ts: datetime) -> bool:
    return SESSION_OPEN <= ts.timetz().replace(tzinfo=None) < SESSION_CLOSE


class LiveBarStore:
    def __init__(self, session_date: date, root_dir: str | Path) -> None:
        self._session_date = session_date
        self._root = Path(root_dir)
        self._bars: dict[str, list[MinuteBar]] = {}
        self._bar_start_cum: dict[str, int | None] = {}  # cum volume before current bar
        self._flushed_through: datetime | None = None  # minutes < this are on disk
        self._coverage_start: datetime | None = None
        self._lock = threading.Lock()

    @property
    def session_date(self) -> date:
        return self._session_date

    @property
    def coverage_start(self) -> datetime | None:
        """Receive time of the first in-session tick. ORB is only trustworthy if
        recording started by 09:15 (see brain.features.live.opening_range)."""
        with self._lock:
            return self._coverage_start

    # ── Ingest ────────────────────────────────────────────────────────────────

    def on_tick(self, tick: Tick) -> None:
        ts = tick.received_at
        if ts.date() != self._session_date or not in_session(ts):
            return
        minute = _floor_minute(ts)
        with self._lock:
            if self._coverage_start is None:
                self._coverage_start = ts
            bars = self._bars.setdefault(tick.symbol, [])
            current = bars[-1] if bars else None
            if current is not None and minute < current.minute:
                return  # late/out-of-order tick for a bar already closed: drop
            if current is None or minute > current.minute:
                start_cum = current.cum_volume if current else tick.volume_traded
                self._bar_start_cum[tick.symbol] = start_cum
                bars.append(
                    MinuteBar(
                        symbol=tick.symbol,
                        exchange=tick.exchange,
                        minute=minute,
                        open=tick.ltp,
                        high=tick.ltp,
                        low=tick.ltp,
                        close=tick.ltp,
                        volume=self._volume(start_cum, tick.volume_traded),
                        cum_volume=tick.volume_traded,
                        vwap=tick.avg_traded_price,
                        ticks=1,
                    )
                )
                return
            start_cum = self._bar_start_cum.get(tick.symbol)
            bars[-1] = replace(
                current,
                high=max(current.high, tick.ltp),
                low=min(current.low, tick.ltp),
                close=tick.ltp,
                volume=(
                    self._volume(start_cum, tick.volume_traded)
                    if tick.volume_traded is not None
                    else current.volume
                ),
                cum_volume=tick.volume_traded
                if tick.volume_traded is not None
                else current.cum_volume,
                vwap=tick.avg_traded_price if tick.avg_traded_price is not None else current.vwap,
                ticks=current.ticks + 1,
            )

    @staticmethod
    def _volume(start_cum: int | None, now_cum: int | None) -> int:
        if start_cum is None or now_cum is None:
            return 0
        return max(0, now_cum - start_cum)

    # ── Read ──────────────────────────────────────────────────────────────────

    def bars(self, symbol: str) -> list[MinuteBar]:
        """All bars for symbol so far, oldest first; the last one may be in progress."""
        with self._lock:
            return list(self._bars.get(symbol, []))

    def symbols(self) -> list[str]:
        with self._lock:
            return sorted(self._bars)

    # ── Persist ───────────────────────────────────────────────────────────────

    def flush(self, now: datetime) -> Path | None:
        """Write every bar whose minute has fully elapsed and is not yet on disk.

        Returns the file written, or None if there was nothing new.
        """
        cutoff = _floor_minute(now)  # bars with minute < cutoff are complete
        with self._lock:
            start = self._flushed_through
            rows = [
                asdict(b)
                for bars in self._bars.values()
                for b in bars
                if b.minute < cutoff and (start is None or b.minute >= start)
            ]
            if not rows:
                return None
            path = self._write(rows, cutoff)
            self._flushed_through = cutoff
        logger.info("minute_bars_flushed rows=%d path=%s", len(rows), path)
        return path

    def _write(self, rows: list[dict], cutoff: datetime) -> Path:
        import pyarrow as pa
        import pyarrow.parquet as pq

        for r in rows:
            r["minute"] = r["minute"].isoformat()  # IST ISO string, explicit offset
        out_dir = self._root / f"date={self._session_date.isoformat()}"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"part-{cutoff.strftime('%H%M')}.parquet"
        pq.write_table(pa.Table.from_pylist(rows), path)
        return path


def read_session_bars(root_dir: str | Path, session_date: date) -> list[dict]:
    """Read back every flushed bar for one session (all part files), oldest first."""
    import pyarrow.parquet as pq

    day_dir = Path(root_dir) / f"date={session_date.isoformat()}"
    if not day_dir.exists():
        return []
    rows: list[dict] = []
    for part in sorted(day_dir.glob("part-*.parquet")):
        rows.extend(pq.read_table(part).to_pylist())
    rows.sort(key=lambda r: (r["minute"], r["symbol"]))
    return rows


class SessionBarRecorder:
    """Holds the current session's LiveBarStore. The orchestrator rolls it each
    morning (start_session) so a process that misses its nightly restart doesn't
    silently drop the next day's ticks on a session-date mismatch."""

    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir)
        self._store: LiveBarStore | None = None
        self._lock = threading.Lock()

    @property
    def store(self) -> LiveBarStore | None:
        with self._lock:
            return self._store

    def start_session(self, session_date: date) -> LiveBarStore:
        with self._lock:
            if self._store is None or self._store.session_date != session_date:
                self._store = LiveBarStore(session_date, self._root)
                logger.info("minute_bars_session_started date=%s", session_date)
            return self._store

    def on_tick(self, tick: Tick) -> None:
        store = self.store
        if store is not None:
            store.on_tick(tick)

    def flush(self, now: datetime) -> Path | None:
        store = self.store
        return store.flush(now) if store is not None else None
