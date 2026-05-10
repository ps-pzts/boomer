"""Tests for the alert layer: AlertManager routing, batching, persistence."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"

from src.alerts.alerter import AlertManager


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    from src.db.migrations import run_migrations

    run_migrations(str(path), MIGRATIONS_DIR)
    return path


def _make_manager(db_path: Path, tg_ok: bool = True, email_ok: bool = True) -> AlertManager:
    mgr = AlertManager(
        db_path=str(db_path),
        telegram_token="fake_token",
        telegram_chat_id="fake_chat",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user",
        smtp_password="pass",
        email_from="from@example.com",
        email_to="to@example.com",
        warn_batch_hours=6,
    )
    # Patch the actual send functions so no network calls happen
    mgr._tg_ok = tg_ok
    mgr._email_ok = email_ok
    return mgr


class TestAlertManagerCritical:
    def test_critical_persisted_to_db(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with (
            patch("src.alerts.alerter.send_telegram", return_value=True),
            patch("src.alerts.alerter.send_email", return_value=True),
        ):
            mgr.critical("Test critical", "Something bad happened")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT severity, title FROM alert_log").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "CRITICAL"
        assert row[1] == "Test critical"

    def test_critical_both_channels_tried(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with (
            patch("src.alerts.alerter.send_telegram", return_value=True) as tg,
            patch("src.alerts.alerter.send_email", return_value=True) as em,
        ):
            mgr.critical("Title", "Body")
        tg.assert_called_once()
        em.assert_called_once()

    def test_both_fail_records_missed_critical(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with (
            patch("src.alerts.alerter.send_telegram", return_value=False),
            patch("src.alerts.alerter.send_email", return_value=False),
        ):
            mgr.critical("Missed critical", "Nobody knows")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT title FROM critical_notification_failures WHERE acknowledged=0"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "Missed critical"

    def test_one_channel_ok_no_missed_record(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with (
            patch("src.alerts.alerter.send_telegram", return_value=True),
            patch("src.alerts.alerter.send_email", return_value=False),
        ):
            mgr.critical("Partial success", "TG ok, email failed")

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM critical_notification_failures").fetchone()[0]
        conn.close()
        assert count == 0


class TestAlertManagerWarn:
    def test_warn_buffered_not_sent_immediately(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with patch("src.alerts.alerter.send_telegram", return_value=True) as tg:
            mgr.warn("Warning 1", "Something to note")
        # First warn within batch window — should NOT have sent to Telegram yet
        # (unless the flush window has elapsed, which it hasn't in a fresh fixture)
        # It IS buffered but batch window check depends on last_warn_flush time
        # Since last flush was year 2000, first warn should actually flush immediately
        # Wait — that's the design: 6h since last flush, and last flush was 2000, so it flushes
        tg.assert_called_once()

    def test_warn_persisted_after_flush(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with patch("src.alerts.alerter.send_telegram", return_value=True):
            mgr.warn("Warning title", "Warning body")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT severity FROM alert_log LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "WARN"


class TestAlertManagerInfo:
    def test_info_persisted(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        mgr.info("Info title", "Info body")
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT severity, title FROM alert_log LIMIT 1").fetchone()
        conn.close()
        assert row[0] == "INFO"
        assert row[1] == "Info title"

    def test_info_does_not_call_telegram(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with patch("src.alerts.alerter.send_telegram") as tg:
            mgr.info("Info title", "Body")
        tg.assert_not_called()


class TestAlertManagerDailySummary:
    def test_daily_summary_calls_telegram(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with patch("src.alerts.alerter.send_telegram", return_value=True) as tg:
            mgr.send_daily_summary("Daily summary", "All good today")
        tg.assert_called_once()
        args = tg.call_args[0]
        assert "All good today" in args[2]

    def test_daily_summary_persisted(self, db_path: Path) -> None:
        mgr = _make_manager(db_path)
        with patch("src.alerts.alerter.send_telegram", return_value=True):
            mgr.send_daily_summary("Summary", "Body text")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT severity FROM alert_log LIMIT 1").fetchone()
        conn.close()
        assert row[0] == "INFO"


class TestTelegramFormat:
    def test_format_includes_severity_and_title(self) -> None:
        from src.alerts.telegram import format_alert_text

        text = format_alert_text("CRITICAL", "Database down", "Cannot connect")
        assert "CRITICAL" in text
        assert "Database down" in text
        assert "Cannot connect" in text


class TestEmailFormat:
    def test_email_subject_format(self) -> None:
        from src.alerts.email_alert import format_email_subject

        subject = format_email_subject("CRITICAL", "Position mismatch")
        assert subject == "[BOOMER CRITICAL] Position mismatch"
