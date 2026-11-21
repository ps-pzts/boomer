"""surveillance_refresh task: the mid-session retry of the ASM/GSM fetch (#50). No real HTTP."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from db.migrations import run_migrations
from orchestrator.tasks_collector import _surveillance_refresh

DAY = "2026-11-19"
_MIGRATIONS = Path(__file__).parents[2] / "migrations"
_FETCHER = "collector.fetchers.surveillance.SurveillanceFetcher"


@pytest.fixture()
def db_path(tmp_path) -> str:
    p = str(tmp_path / "r.db")
    run_migrations(p, _MIGRATIONS)
    return p


@pytest.fixture()
def alerter():
    fake = MagicMock()
    with patch("alerts.alerter.get_alerter", return_value=fake):
        yield fake


def _have(db_path: str, list_type: str, list_date: str = DAY) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO surveillance_list (list_date, list_type, symbol, observed_at)"
        " VALUES (?, ?, 'X', ?)",
        (list_date, list_type, f"{list_date}T07:30:00"),
    )
    conn.commit()
    conn.close()


def test_fetches_both_lists_when_neither_is_present_and_alerts(db_path, tmp_path, alerter):
    with patch(_FETCHER) as cls:
        cls.return_value.refresh.return_value = {"ASM": 40, "GSM": 12}
        _surveillance_refresh(DAY, 1, db_path=db_path, archive_dir=str(tmp_path))
    cls.return_value.refresh.assert_called_once_with(("ASM", "GSM"))
    assert alerter.info.call_args.args[0] == "ASM/GSM list fetched mid-session"


def test_fetches_only_the_missing_list(db_path, tmp_path, alerter):
    _have(db_path, "ASM")
    with patch(_FETCHER) as cls:
        cls.return_value.refresh.return_value = {"GSM": 12}
        _surveillance_refresh(DAY, 1, db_path=db_path, archive_dir=str(tmp_path))
    cls.return_value.refresh.assert_called_once_with(("GSM",))


def test_nothing_to_do_when_both_are_current(db_path, tmp_path, alerter):
    _have(db_path, "ASM")
    _have(db_path, "GSM")
    with patch(_FETCHER) as cls:
        _surveillance_refresh(DAY, 1, db_path=db_path, archive_dir=str(tmp_path))
    cls.assert_not_called()
    alerter.info.assert_not_called()


def test_yesterdays_list_does_not_count_as_current(db_path, tmp_path, alerter):
    _have(db_path, "ASM", "2026-11-18")
    _have(db_path, "GSM", "2026-11-18")
    with patch(_FETCHER) as cls:
        cls.return_value.refresh.return_value = {}
        _surveillance_refresh(DAY, 1, db_path=db_path, archive_dir=str(tmp_path))
    cls.return_value.refresh.assert_called_once_with(("ASM", "GSM"))
    alerter.info.assert_not_called()  # NSE still down: no false "fetched" alert
