"""Daily Kite check results and the session gate they feed (#85)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from db.migrations import run_migrations
from executor import kite_checks as kc
from executor.preflight import CheckReport, CheckResult
from tests.executor.kite_fake import FakeKite

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 19)
_MIGRATIONS = Path(__file__).parents[2] / "migrations"


def report(kind: str, ok: bool, at: str = "2026-11-19T09:15:05") -> CheckReport:
    return CheckReport(kind, (CheckResult("x", ok, "detail"),), at)


def record_passes(conn: sqlite3.Connection, day: date = DAY) -> None:
    """Test helper: today's pre-flight and canary both passed."""
    for kind in kc.CHECKS:
        kc.record(conn, day, report(kind, True, f"{day.isoformat()}T09:15:05"), source="test")


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    p = str(tmp_path / "k.db")
    run_migrations(p, _MIGRATIONS)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c


class TestGate:
    def test_nothing_run_yet(self, db):
        assert kc.gate(db, DAY) == "preflight_pending"

    def test_preflight_passed_canary_pending(self, db):
        kc.record(db, DAY, report("preflight", True), source="scheduled")
        assert kc.gate(db, DAY) == "canary_pending"

    def test_both_passed(self, db):
        record_passes(db)
        assert kc.gate(db, DAY) is None

    def test_failure_wins_over_pending(self, db):
        kc.record(db, DAY, report("canary", False), source="scheduled")
        assert kc.gate(db, DAY) == "canary_failed"

    def test_preflight_failure_reported_first(self, db):
        kc.record(db, DAY, report("preflight", False), source="scheduled")
        kc.record(db, DAY, report("canary", False), source="scheduled")
        assert kc.gate(db, DAY) == "preflight_failed"

    def test_latest_run_wins(self, db):
        kc.record(db, DAY, report("preflight", False), source="scheduled")
        kc.record(db, DAY, report("canary", False), source="scheduled")
        kc.record(db, DAY, report("preflight", True), source="operator")
        kc.record(db, DAY, report("canary", True), source="operator")
        assert kc.gate(db, DAY) is None
        kc.record(db, DAY, report("canary", False), source="operator")
        assert kc.gate(db, DAY) == "canary_failed"

    def test_yesterdays_pass_does_not_count(self, db):
        record_passes(db, date(2026, 11, 18))
        assert kc.gate(db, DAY) == "preflight_pending"

    def test_row_keeps_the_report(self, db):
        kc.record(db, DAY, report("canary", False), source="operator")
        row = db.execute("SELECT * FROM kite_daily_checks").fetchone()
        assert (row["session_date"], row["check_name"], row["ok"]) == ("2026-11-19", "canary", 0)
        assert row["source"] == "operator" and row["ran_at"] == "2026-11-19T09:15:05"
        assert json.loads(row["detail_json"])["checks"][0]["detail"] == "detail"

    def test_unknown_check_rejected(self, db):
        with pytest.raises(ValueError):
            kc.record(db, DAY, report("nope", True), source="test")


class _Alerter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def critical(self, title, body, source_task_id=None):
        self.sent.append(("critical", title))

    def info(self, title, body, source_task_id=None):
        self.sent.append(("info", title))


class TestRunAndRecord:
    NOW = datetime(2026, 11, 19, 9, 15, 5, tzinfo=IST)

    def _run(self, db_path, kind, broker, alerter, tmp_path):
        return kc.run_and_record(
            db_path,
            kind,
            broker,
            feed=None,
            alerter=alerter,
            source="scheduled",
            canary_config=kc.CanaryConfig(recordings_dir=str(tmp_path / "rec")),
            clock=lambda: self.NOW,
            sleep=lambda s: None,
        )

    def test_pass_is_recorded_without_alert(self, db, tmp_path):
        path = db.execute("PRAGMA database_list").fetchone()[2]
        alerter = _Alerter()
        rep = self._run(path, "canary", FakeKite().broker(), alerter, tmp_path)
        assert rep.ok and kc.gate(db, DAY) == "preflight_pending"
        assert alerter.sent == []

    def test_failure_is_recorded_and_alerted(self, db, tmp_path):
        path = db.execute("PRAGMA database_list").fetchone()[2]
        fk = FakeKite()
        fk.reject_place = {"BUY"}
        alerter = _Alerter()
        rep = self._run(path, "canary", fk.broker(), alerter, tmp_path)
        assert not rep.ok and kc.gate(db, DAY) == "canary_failed"
        assert alerter.sent == [("critical", "Kite canary FAILED — trading blocked today")]

    def test_no_kite_broker_is_a_failure(self, db, tmp_path):
        path = db.execute("PRAGMA database_list").fetchone()[2]
        alerter = _Alerter()
        rep = self._run(path, "preflight", None, alerter, tmp_path)
        assert not rep.ok and "no Kite broker" in rep.failures()[0]
        assert kc.gate(db, DAY) == "preflight_failed"
        assert alerter.sent[0][0] == "critical"

    def test_kite_broker_picked_from_the_list(self):
        from executor.brokers.paper_broker import PaperBroker

        kite = FakeKite().broker()
        paper = object.__new__(PaperBroker)
        assert kc.kite_broker([paper, kite]) is kite
        assert kc.kite_broker([paper]) is None
        assert kc.kite_broker([]) is None

    def test_kite_broker_from_the_src_import_tree_is_found(self):
        """The orchestrator builds `src.executor.brokers.kite_broker.KiteBroker`."""
        from src.executor.brokers.kite_broker import KiteBroker as SrcKite

        b = SrcKite()
        assert kc.kite_broker([b]) is b
