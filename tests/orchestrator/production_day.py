"""A whole paper trading day through the production wiring (#116).

Unlike tests/integration (its own Harness) and tests/executor `Day` (hand-built
SessionController), this rig builds the system the way `orchestrator.main()` does:
`build_session` + `build_decision_cycle` + `Orchestrator`, on a real ManualFeed and
a real SessionBarRecorder, with the real registered tasks dispatched by the real
Scheduler. Only the wall clock is fake (`FakeClock.install`).

Seeded (the parts that need the network or the batch pipeline, run earlier in
production): risk_config, capital ledger, the day's Kite checks, one watchlist
entry, its batch features, and one news filing. Not run (see SKIPPED_TASKS).
"""

from __future__ import annotations

import datetime as dt
import importlib
import pkgutil
import sqlite3
import sys
from datetime import datetime as RealDatetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DAY = dt.date(2026, 11, 19)  # a Thursday
MIGRATIONS = Path(__file__).parents[2] / "migrations"
SRC = Path(__file__).parents[2] / "src"

# Registered tasks the day does not run, and why. Everything else runs for real.
SKIPPED_TASKS = {
    "nightly_eod_collector": "network collectors, runs 07:30 in production",
    "surveillance_refresh": "fetches the NSE/BSE ASM-GSM lists over HTTP",
    "early_morning_data_check": "needs the nightly collector's output",
    "morning_batch_features": "batch pipeline: its output (features) is seeded",
    "intraday_watchlist": "needs the batch pipeline: the watchlist is seeded",
    "kite_preflight": "needs a real KiteBroker: today's passing result is seeded",
    "kite_canary": "needs a real KiteBroker: today's passing result is seeded",
    "morning_heartbeat": "Telegram ping, not part of the trading path",
    "eod_reconciliation": "16:00, after the day under test",
    "weekly_harvest_check": "Friday 16:30",
    "nightly_backup": "23:00",
    "nightly_health_check": "01:45",
}

# Packages whose every module is imported before the clock is patched, so none can be
# missed by a fixed list (a split or a new module is picked up by itself).
_PACKAGES = ["executor", "brain", "capital", "orchestrator", "observability", "alerts"]
_EXTRA = ["collector.live_bars"]


def _import_all() -> None:
    names = list(_EXTRA)
    for pkg in _PACKAGES:
        names.append(pkg)
        names += [m.name for m in pkgutil.walk_packages([str(SRC / pkg)], f"{pkg}.")]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception:  # an optional dependency missing: that module is not in play
            continue


def src_modules() -> list:
    return [
        m
        for m in list(sys.modules.values())
        if str(getattr(m, "__file__", "") or "").startswith(str(SRC))
    ]


class FakeClock:
    """`datetime.now(tz)` everywhere in src returns `self.now` (IST)."""

    def __init__(self, now: RealDatetime) -> None:
        self.now = now

    def install(self, monkeypatch) -> None:
        clock, real = self, RealDatetime

        class _Meta(type):
            def __instancecheck__(cls, obj: object) -> bool:
                return isinstance(obj, real)

        class FakeDatetime(real, metaclass=_Meta):
            def __new__(cls, *args, **kwargs):  # always hand back a real datetime
                return real(*args, **kwargs)

            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)

        _import_all()
        monkeypatch.setattr(dt, "datetime", FakeDatetime)  # `import datetime` / local imports
        for mod in list(sys.modules.values()):  # `from datetime import datetime`
            f = getattr(mod, "__file__", None)
            if f and str(f).startswith(str(SRC)) and getattr(mod, "datetime", None) is real:
                monkeypatch.setattr(mod, "datetime", FakeDatetime)

    @staticmethod
    def on_real_clock() -> list[str]:
        """Source modules that still resolve `datetime` to the real class (call while the
        clock is installed): their `datetime.now()` would read the wall clock."""
        return sorted(
            m.__name__ for m in src_modules() if getattr(m, "datetime", None) is RealDatetime
        )

    def at(self, hh: int, mm: int) -> RealDatetime:
        self.now = RealDatetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=IST)
        return self.now


class Alerts:
    """Stands in for alerts.alerter.AlertManager: records instead of sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def _rec(self, level: str, title: str, body: str, **_: object) -> None:
        self.sent.append((level, title, body))

    def info(self, title, body, source_task_id=None):
        self._rec("info", title, body)

    def warn(self, title, body, source_task_id=None):
        self._rec("warn", title, body)

    def critical(self, title, body, source_task_id=None):
        self._rec("critical", title, body)

    def send_daily_summary(self, title, body):
        self._rec("summary", title, body)

    def titles(self, level: str) -> list[str]:
        return [t for lv, t, _ in self.sent if lv == level]


def seed(db: str) -> None:
    """A fresh database with everything the 08:45 day needs from earlier jobs."""
    from brain.feature_store import FeatureStore
    from brain.watchlist import WatchlistEntry, WatchlistStore
    from capital.state import CapitalStateManager
    from db.migrations import run_migrations
    from executor import kite_checks as kc
    from orchestrator.bootstrap import seed_risk_config
    from tests.executor.test_kite_checks import report

    run_migrations(db, MIGRATIONS)
    seed_risk_config(db, DAY)
    CapitalStateManager(db).initialise(
        __import__("decimal").Decimal("1000000"), DAY - dt.timedelta(days=1)
    )
    conn = sqlite3.connect(db)
    for kind in kc.CHECKS:  # 08:55 pre-flight and 09:15 canary passed
        kc.record(conn, DAY, report(kind, True, f"{DAY.isoformat()}T09:15:05"), source="test")
    conn.close()
    WatchlistStore(db).save(DAY, [WatchlistEntry("TCS", "NSE", 1, "batch_score", 0.8, "long", 5e9)])
    batch = dict(  # the worked ORB example in tests/brain/test_intraday_decision.py
        atr_14d=20.0,
        overnight_news_sentiment=0.5,
        fo_oi_overnight_change_pct=5.0,
        beta_20d=1.0,
        avg_daily_volume_20d=1e6,
        avg_traded_value_20d=5e9,
    )
    fs, prev = FeatureStore(db), DAY - dt.timedelta(days=1)
    for name, value in batch.items():
        fs.write_feature("TCS", "NSE", name, value, valid_from=prev, source_max_observed_at=prev)
    conn = sqlite3.connect(db)
    conn.execute(  # a filing observed 09:45 IST (collector format: IST wall clock + "Z")
        "INSERT INTO filings (filing_id, raw_id, parser_version, stock_symbol, exchange,"
        " filing_date, observed_at, category, headline) VALUES"
        " ('f1','r1','v1','TCS','NSE',?,?,'other','order win')",
        (DAY.isoformat(), f"{DAY.isoformat()}T09:45:00Z"),
    )
    conn.commit()
    conn.close()


def build(tmp_path: Path, alerts: Alerts):
    """Boot the way orchestrator.main() does. Returns a namespace with the pieces."""
    from collector.live_bars import SessionBarRecorder
    from executor.brokers.feeds.manual_feed import ManualFeed
    from orchestrator.execution import build_decision_cycle, build_session
    from orchestrator.orchestrator import Orchestrator, _start_event_stream

    db = str(tmp_path / "boomer.db")
    seed(db)
    feed = ManualFeed()
    recorder = SessionBarRecorder(str(tmp_path / "minute_bars"))
    feed.on_tick(recorder.on_tick)  # _build_bar_recorder
    emitter = _start_event_stream(db, "paper", feed)
    session = build_session(db, None, feed, [], alerter=alerts)  # mode None: resolved per day
    cycle = build_decision_cycle(db, session, feed, recorder)
    orc = Orchestrator(
        db_path=db,
        archive_dir=str(tmp_path / "archive"),
        backup_dir=str(tmp_path / "backup"),
        feed=feed,
        bar_recorder=recorder,
        session=session,
        cycle=cycle,
    )
    return SimpleNamespace(
        db=db, feed=feed, recorder=recorder, emitter=emitter, session=session, cycle=cycle, orc=orc
    )


def dispatch_minute(orc, now: RealDatetime, log: list[tuple[str, str]]) -> None:
    """One pass of Orchestrator._loop's body, run inline instead of on threads."""
    run_date = orc._scheduler.current_run_date(now)
    for task_id, task_def in orc._tasks.items():
        if task_id in SKIPPED_TASKS:
            continue
        should, _ = orc._scheduler.should_run(task_def, now, run_date)
        if should:
            log.append((now.strftime("%H:%M"), task_id))
            orc._run_task(task_def, run_date)
