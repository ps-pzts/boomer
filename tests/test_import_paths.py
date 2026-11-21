"""One import path per module (#94).

Every first-party package is imported by its bare name (`executor.models`), never as
`src.executor.models`. The `src.` prefix creates a second module object for the same file:
isinstance silently fails across the two copies and module-level state (singletons, locks)
exists twice. This is how every production entrypoint starts too (`python -m
orchestrator.orchestrator`, `uvicorn dashboard.app:app`; see ops/systemd, dev.sh, Dockerfile).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
PACKAGES = sorted(p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists())
SCANNED = [*SRC.rglob("*.py"), *(ROOT / "tests").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]


def _is_src_path(dotted: str) -> bool:
    return dotted == "src" or dotted.startswith("src.")


def src_prefix_offences(source: str) -> list[int]:
    """Line numbers that import via `src.`: import statements and dotted strings
    (mock.patch targets, importlib names, `-m` style module paths)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(_is_src_path(a.name) for a in node.names):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _is_src_path(node.module):
                lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            head = node.value.split(".", 2)
            if len(head) >= 2 and head[0] == "src" and head[1] in PACKAGES:
                lines.append(node.lineno)
    return sorted(set(lines))


def escaping_relative_imports(source: str, module: str) -> list[int]:
    """Relative imports that climb out of the top-level package (`from ..banner import x`
    inside `orchestrator.orchestrator`): they only work under the `src.` root."""
    package_depth = module.count(".")  # orchestrator.orchestrator -> 1 package level
    return [
        n.lineno
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.ImportFrom) and n.level - 1 >= max(package_depth, 1)
    ]


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts[-1] = "pkg_init"  # an __init__ is its own package: same depth as a sibling
    return ".".join(parts)


class TestNoSrcPrefix:
    def test_no_file_imports_through_the_src_prefix(self):
        offences = []
        for path in SCANNED:
            if path == Path(__file__):
                continue
            offences += [
                f"{path.relative_to(ROOT)}:{n}"
                for n in src_prefix_offences(path.read_text(encoding="utf-8"))
            ]
        assert offences == [], (
            "Import first-party code without the `src.` prefix (#94): " + ", ".join(offences)
        )

    def test_checker_catches_every_form(self):
        assert src_prefix_offences("from src.executor import models") == [1]
        assert src_prefix_offences("import src.executor.models") == [1]
        assert src_prefix_offences("def f():\n    from src.db.migrations import x") == [2]
        assert src_prefix_offences("patch('src.alerts.alerter.send_telegram')") == [1]
        assert src_prefix_offences("importlib.import_module('src.executor.main')") == [1]

    def test_checker_allows_the_valid_forms(self):
        assert src_prefix_offences("from executor import models\nimport executor.models") == []
        assert src_prefix_offences("src = 1\nx = src.value\nPath('src/executor')") == []
        assert src_prefix_offences("patch('alerts.alerter.send_telegram')") == []

    def test_no_relative_import_escapes_its_top_level_package(self):
        offences = []
        for path in SRC.rglob("*.py"):
            if path.parent == SRC:
                continue  # top-level modules (banner.py) have no package to climb
            offences += [
                f"{path.relative_to(ROOT)}:{n}"
                for n in escaping_relative_imports(
                    path.read_text(encoding="utf-8"), _module_name(path)
                )
            ]
        assert offences == [], "Relative import leaves its package: " + ", ".join(offences)

    def test_escape_checker_catches_and_allows(self):
        assert escaping_relative_imports("from ..banner import x", "orchestrator.orchestrator")
        assert not escaping_relative_imports("from .models import x", "orchestrator.orchestrator")
        assert not escaping_relative_imports("from ..a import x", "alerts.commands.reads")


_SMOKE = """
import importlib, pathlib, sys
failed = []
for p in sorted(pathlib.Path("{src}").rglob("*.py")):
    parts = list(p.relative_to("{src}").with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        continue
    try:
        importlib.import_module(".".join(parts))
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] in {pkgs!r}:
            failed.append((".".join(parts), repr(exc)))  # first-party: a real break
    except RuntimeError as exc:
        if "python-multipart" not in str(exc):  # optional dashboard dependency
            failed.append((".".join(parts), repr(exc)))
    except Exception as exc:
        failed.append((".".join(parts), repr(exc)))
dup = sorted(m for m in sys.modules if m == "src" or m.startswith("src."))
from executor.models import OrderStatus
from executor.brokers.kite_broker import KiteBroker
import orchestrator.execution, orchestrator.orchestrator, alerts.telegram_bot
import observability.telegram_feed
assert sys.modules["executor.models"].OrderStatus is OrderStatus
assert sys.modules["executor.brokers.kite_broker"].KiteBroker is KiteBroker
print(failed, dup)
"""


class TestOneModuleObject:
    def test_every_module_imports_as_production_starts_it_with_no_src_copy(self, tmp_path):
        """Fresh interpreter, only `src` on the path (as the editable install / PYTHONPATH
        gives production), cwd elsewhere so the repo root is NOT importable: a `src.`
        import anywhere would fail loudly here."""
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                _SMOKE.format(src=SRC, pkgs=sorted({*PACKAGES, "banner", "src"})),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": str(SRC), "PATH": "", "BOOMER_DB_PATH": str(tmp_path / "b.db")},
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == "[] []", out.stdout

    def test_classes_are_identical_across_in_repo_routes(self):
        import executor.models as m1
        import orchestrator.execution  # noqa: F401  (the orchestrator's own import chain)
        from executor import models as m2
        from executor.brokers import kite_broker as kb1
        from executor.brokers.kite_broker import KiteBroker

        assert m1 is m2 is sys.modules["executor.models"]
        assert m1.OrderStatus is m2.OrderStatus
        assert kb1.KiteBroker is KiteBroker
        assert not [k for k in sys.modules if k == "src" or k.startswith("src.")]

    def test_kite_broker_isinstance_check_works_for_a_real_kite_broker(self):
        from executor import kite_checks
        from executor.brokers.kite_broker import KiteBroker

        broker = KiteBroker()
        assert kite_checks.kite_broker([broker]) is broker
