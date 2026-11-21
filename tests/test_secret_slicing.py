"""Secrets hygiene (#142): no log/print/alert call may show a slice of a token/secret/password."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SENSITIVE = re.compile(r"token|secret|password", re.I)
SINKS = {
    "print", "debug", "info", "warning", "error", "exception", "critical",
    "_notify", "send_telegram", "rows", "header", "note",
}  # fmt: skip


def _name(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")


def slicing_lines(source: str) -> list[int]:
    hits = []
    for call in ast.walk(ast.parse(source)):
        if isinstance(call, ast.Call) and _name(call.func) in SINKS:
            for arg in [*call.args, *(k.value for k in call.keywords)]:
                for n in ast.walk(arg):
                    if (
                        isinstance(n, ast.Subscript)
                        and isinstance(n.slice, ast.Slice)
                        and SENSITIVE.search(_name(n.value))
                    ):
                        hits.append(n.lineno)
    return sorted(set(hits))  # nested sinks (alert(rows(..))) see the same slice twice


def test_detector_sees_leaks_and_ignores_safe_code():
    assert slicing_lines('print(f"x {token[:12]}")') == [1]
    assert slicing_lines('logger.debug("t=%s", request_token[:8])') == [1]
    assert slicing_lines('_notify("t", rows([("Token", f"{self.api_secret[:4]}")]))') == [1]
    assert slicing_lines('print(user[:3])\nx = token[:4]\nlogger.info("%s", token)') == []


def test_no_secret_slice_in_log_print_or_alert():
    files = [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
    offenders = [f"{f.relative_to(ROOT)}:{n}" for f in files for n in slicing_lines(f.read_text())]
    assert not offenders, f"secret slice in log/print/alert: {offenders}"
