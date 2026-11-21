"""Every process entrypoint we ship must exist and be runnable (#109).

Parses each `python -m <module>` / `uvicorn <module>:<attr>` target out of ops/systemd/*.service,
dev.sh, the Dockerfile CMD and ops/runbook.md, then asserts the module is importable and, when
run with `-m`, has a `__main__` guard. Also pins the design decision: the executor has no
process of its own (the orchestrator owns the feed and the one SessionController).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SOURCES = [
    *sorted((ROOT / "ops" / "systemd").glob("*.service")),
    ROOT / "dev.sh",
    ROOT / "Dockerfile",
    ROOT / "ops" / "runbook.md",
]
_PY_M = re.compile(r"python3?(?:\.\d+)?\s+-m\s+([A-Za-z_][\w.]*)")
_UVICORN = re.compile(r"uvicorn\s+([A-Za-z_][\w.]*):(\w+)")


def _docker_cmd_text(text: str) -> str:
    """Flatten a JSON-array `CMD [...]` line so the uvicorn regex can see it."""
    out = []
    for line in text.splitlines():
        if line.startswith("CMD "):
            try:
                out.append(" ".join(json.loads(line[4:])))
            except ValueError:
                out.append(line)
    return "\n".join(out)


def _targets() -> list[tuple[str, str, str | None]]:
    """(source file, module, attr-or-None) for every launch target."""
    found = []
    for path in SOURCES:
        text = path.read_text()
        for m in _PY_M.finditer(text):
            found.append((path.name, m.group(1), None))
        for m in _UVICORN.finditer(text + "\n" + _docker_cmd_text(text)):
            found.append((path.name, m.group(1), m.group(2)))
    return sorted(set(found), key=lambda t: (t[0], t[1], t[2] or ""))


TARGETS = _targets()


def test_targets_are_discovered():
    names = {t[1] for t in TARGETS}
    # Guards against the parser silently matching nothing.
    assert {"orchestrator.orchestrator", "alerts.telegram_bot", "dashboard.app"} <= names


@pytest.mark.parametrize(("source", "module", "attr"), TARGETS)
def test_launch_target_exists_and_is_runnable(source, module, attr):
    spec = importlib.util.find_spec(module)
    assert spec is not None, f"{source} launches {module}, which does not exist"
    if attr is not None:  # uvicorn module:app
        # Static check: importing the app would need every runtime dependency installed.
        tree = ast.parse(Path(spec.origin).read_text())
        names = {
            t.id
            for n in tree.body
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        assert attr in names, f"{module} defines no module-level {attr}"
    else:  # python -m needs a __main__ guard or it exits silently doing nothing
        src = Path(spec.origin).read_text()
        assert re.search(r"if __name__ == [\"']__main__[\"']", src), (
            f"{source} runs `python -m {module}` but it has no __main__ guard"
        )


def test_executor_has_no_process_of_its_own():
    """One SessionController per DB: the executor runs inside the orchestrator."""
    assert not (ROOT / "ops" / "systemd" / "boomer-executor.service").exists()
    for path in [*SOURCES, ROOT / ".github" / "workflows" / "cd.yml"]:
        text = path.read_text()
        assert "boomer-executor" not in text, path.name
        assert not re.search(r"-m\s+executor\.", text), path.name
