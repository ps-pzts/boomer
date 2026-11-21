"""CLAUDE.md §9: only src/executor/brokers/ imports the Kite SDK (#126).

tests/ may fake the SDK; scripts/ are scanned too since they run in production ops.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
BROKERS = ROOT / "src" / "executor" / "brokers"


def sdk_import_lines(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        if any(n.split(".")[0] == "kiteconnect" for n in names):
            lines.append(node.lineno)
    return lines


def test_detector_sees_every_import_form():
    src = "import kiteconnect\nfrom kiteconnect import x\nfrom kiteconnect.exceptions import y\n"
    assert sdk_import_lines(src) == [1, 2, 3]
    assert sdk_import_lines("import kiteconnected\nfrom foo import kiteconnect\n") == []


def test_no_sdk_import_outside_brokers():
    files = [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    offenders = [
        f"{f.relative_to(ROOT)}:{n}"
        for f in files
        if BROKERS not in f.parents
        for n in sdk_import_lines(f.read_text())
    ]
    assert not offenders, f"kiteconnect imported outside executor/brokers/: {offenders}"
