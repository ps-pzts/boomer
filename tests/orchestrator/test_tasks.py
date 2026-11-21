"""Tests for the task registry in src/orchestrator/tasks.py."""

from __future__ import annotations

from orchestrator.tasks import build_task_registry


def _registry() -> dict:
    return build_task_registry(db_path=":memory:", archive_dir="/tmp/a", backup_dir="/tmp/b")


def test_no_gtt_dispatch_task_registered():
    # Regression: gtt_dispatch placed CNC OCO-GTTs valid 365 days for intraday
    # recommendations, turning intraday signals into year-long delivery positions.
    # Boomer is MIS-only; no scheduled task may place GTTs.
    tasks = _registry()
    assert "gtt_dispatch" not in tasks
    assert not any("gtt" in task_id for task_id in tasks)


def test_registry_keys_match_task_ids():
    for key, td in _registry().items():
        assert key == td.task_id


def test_no_task_depends_on_an_unregistered_task():
    tasks = _registry()
    for td in tasks.values():
        for dep in td.dependencies:
            assert dep in tasks, f"{td.task_id} depends on missing task {dep}"
