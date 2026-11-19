"""Daily Kite checks: run, persist, and gate the session on them (#85).

Pre-flight (08:55, `executor/preflight.py`) and the order canary (09:15,
`executor/canary.py`) both write a row to `kite_daily_checks`. The Session
machine may not reach TRADING until the latest run of each passed today; a
failure halts the session (`preflight_failed` / `canary_failed`), which only a
passing operator re-run (`/canary`) clears.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time as _time
from collections.abc import Callable
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from executor.canary import CanaryConfig, run_canary
from executor.preflight import CheckReport, CheckResult, run_preflight

__all__ = [
    "CHECKS",
    "CanaryConfig",
    "gate",
    "kite_broker",
    "kite_broker_from_env",
    "record",
    "rerun",
    "run_and_record",
]

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
CHECKS = ("preflight", "canary")


def record(conn: sqlite3.Connection, day: date, report: CheckReport, *, source: str) -> None:
    if report.kind not in CHECKS:
        raise ValueError(f"unknown Kite check {report.kind!r}")
    conn.execute(
        "INSERT INTO kite_daily_checks (session_date, check_name, ok, detail_json, source,"
        " ran_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            day.isoformat(),
            report.kind,
            int(report.ok),
            json.dumps(report.as_dict(), default=str),
            source,
            report.ran_at,
        ),
    )
    conn.commit()


def gate(conn: sqlite3.Connection, day: date) -> str | None:
    """None when today's latest pre-flight and canary both passed; otherwise the
    reason trading is blocked. A failure outranks a check that hasn't run yet."""
    latest: dict[str, bool] = {}
    for check in CHECKS:
        row = conn.execute(
            "SELECT ok FROM kite_daily_checks WHERE session_date=? AND check_name=?"
            " ORDER BY id DESC LIMIT 1",
            (day.isoformat(), check),
        ).fetchone()
        if row is not None:
            latest[check] = bool(row[0])
    for check in CHECKS:
        if latest.get(check) is False:
            return f"{check}_failed"
    for check in CHECKS:
        if check not in latest:
            return f"{check}_pending"
    return None


def kite_broker(brokers: list) -> object | None:
    """The real KiteBroker; never the paper broker (the canary runs on paper days too).
    Matched by class name, not isinstance: the orchestrator imports `src.executor...`
    and this module `executor...`, two distinct class objects for the same broker.
    #86: paper is a KiteBroker too (on KiteSim), so the broker's own id must be kite."""
    return next(
        (
            b
            for b in brokers
            if type(b).__name__ == "KiteBroker" and str(getattr(b, "broker_id", "")) == "kite"
        ),
        None,
    )


def run_and_record(
    db_path: str,
    kind: str,
    broker: object | None,
    *,
    feed: object | None = None,
    alerter: object | None = None,
    source: str = "scheduled",
    canary_config: CanaryConfig | None = None,
    probe_symbol: str = "INFY",
    clock: Callable[[], datetime] = lambda: datetime.now(IST),
    sleep: Callable[[float], None] = _time.sleep,
) -> CheckReport:
    """Run one check against the real Kite broker, persist it, alert on failure."""
    if kind not in CHECKS:
        raise ValueError(f"unknown Kite check {kind!r}")
    if broker is None:
        report = CheckReport(
            kind,
            (CheckResult("broker", False, "no Kite broker configured (KITE_API_KEY/TOKEN)"),),
            clock().replace(tzinfo=None).isoformat(),
        )
    elif kind == "preflight":
        report = run_preflight(broker, feed, symbol=probe_symbol, clock=clock, sleep=sleep)
    else:
        report = run_canary(broker, canary_config, clock=clock, sleep=sleep)
    day = datetime.fromisoformat(report.ran_at).date()
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        record(conn, day, report, source=source)
    finally:
        conn.close()
    log = logger.info if report.ok else logger.error
    log("kite_check kind=%s ok=%s source=%s %s", kind, report.ok, source, report.failures())
    if not report.ok and alerter is not None:
        try:
            alerter.critical(  # type: ignore[attr-defined]
                f"Kite {kind} FAILED — trading blocked today",
                "Failed: "
                + "; ".join(report.failures())
                + ". The session will not trade until a re-run passes (Telegram /canary).",
                source_task_id=f"kite_{kind}",
            )
        except Exception:
            logger.exception("kite_check_alert_failed kind=%s", kind)
    return report


# The canary's orders only rest in the normal market: NSE pre-open rejects stop
# orders and MIS entries close at 15:20.
CANARY_WINDOW = (time(9, 15), time(15, 20))


def rerun(
    db_path: str,
    broker: object | None,
    *,
    alerter: object | None = None,
    canary_config: CanaryConfig | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(IST),
    sleep: Callable[[float], None] = _time.sleep,
) -> list[CheckReport]:
    """Operator re-run (Telegram /canary): pre-flight, then the canary if the
    pre-flight passed and the market is in its normal session. Passing both lets a
    session halted by a Kite check resume through RECONCILING."""
    kw = {"alerter": alerter, "source": "operator", "clock": clock, "sleep": sleep}
    reports = [run_and_record(db_path, "preflight", broker, **kw)]
    now = clock()
    in_window = now.weekday() < 5 and CANARY_WINDOW[0] <= now.time() < CANARY_WINDOW[1]
    if reports[0].ok and in_window:
        reports.append(run_and_record(db_path, "canary", broker, canary_config=canary_config, **kw))
    return reports


def kite_broker_from_env(env_path: Path | str | None = None) -> object | None:
    """A KiteBroker for a process that didn't build one (the Telegram bot). The 08:45
    refresh writes the day's token to .env, so it is read from there first."""
    from executor.brokers.kite_broker import KiteBroker

    path = Path(env_path) if env_path else Path(__file__).parents[2] / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in ("KITE_API_KEY", "KITE_ACCESS_TOKEN") and value.strip():
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    if not (os.environ.get("KITE_API_KEY") and os.environ.get("KITE_ACCESS_TOKEN")):
        return None
    broker = KiteBroker()
    broker.authenticate()
    return broker
