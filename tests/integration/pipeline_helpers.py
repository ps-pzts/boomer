"""Shared helpers for the full-pipeline integration tests (split from
tests/test_integration_full_pipeline.py, #117)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS = [
    "migrations/0001_initial_schema.sql",
    "migrations/0002_collector_schema.sql",
    "migrations/0003_brain_schema.sql",
    "migrations/0004_executor_schema.sql",
    "migrations/0005_orchestrator_schema.sql",
]


def _apply_all_migrations(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    for path in MIGRATIONS:
        conn.executescript(Path(path).read_text())
    conn.close()


def _make_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "test.db")
    _apply_all_migrations(db_path)
    return db_path
