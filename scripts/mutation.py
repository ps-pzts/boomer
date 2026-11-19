"""Small mutation tester for the money-critical modules (#89). Dev tool, not in CI.

    python scripts/mutation.py src/executor/trailing.py tests/executor/test_trailing.py
    python scripts/mutation.py src/executor/brokers/kite_broker.py tests/executor/brokers \
        --func map_kite_status

For each mutation site in the module (one change at a time) it rewrites the file, runs the
given tests, and restores the file. A mutant is KILLED when a test fails (or times out) and
SURVIVES when every test still passes: the tests would not notice that bug. Survivors are
listed with line, original and mutated code; some are equivalent (no behaviour change) and
are marked in the report by hand.

Operators: comparison swaps (== != < <= > >=), arithmetic (+ - * /), and/or, `not` removed,
True/False flipped, numeric constants +1, `if`/`while` condition negated, `return x` -> None.
Docstrings and annotations are never mutated. The file is always restored.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_CMP = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}
_ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


@dataclass
class Mutant:
    index: int
    line: int
    kind: str
    original: str
    mutated: str
    status: str = "pending"  # killed | survived | timeout


def _src(node: ast.AST) -> str:
    return ast.unparse(node).replace("\n", " ")[:90]


class _Sites(ast.NodeVisitor):
    """Collects (kind, node, apply) for every mutation site, in source order."""

    def __init__(self) -> None:
        self.sites: list[tuple[str, ast.AST]] = []

    def visit_Expr(self, node: ast.Expr) -> None:  # docstrings and bare strings
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:  # annotations
        return

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for stmt in node.body:
            self.visit(stmt)
        for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            self.visit(d)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Compare(self, node: ast.Compare) -> None:
        if any(type(op) in _CMP for op in node.ops):
            self.sites.append(("compare", node))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if type(node.op) in _ARITH:
            self.sites.append(("arith", node))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.sites.append(("boolop", node))
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, ast.Not):
            self.sites.append(("not", node))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool) or (
            isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        ):
            self.sites.append(("const", node))

    def visit_If(self, node: ast.If) -> None:
        self.sites.append(("cond", node))
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.sites.append(("cond", node))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            self.sites.append(("return", node))
        self.generic_visit(node)


def _restrict(tree: ast.Module, func: str | None) -> ast.AST:
    if not func:
        return tree
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == func:
            return n
    raise SystemExit(f"no function/class named {func!r}")


def _mutate(tree: ast.Module, target: ast.AST, kind: str, n: int) -> tuple[ast.Module, Mutant]:
    """A copy of `tree` with the n-th site of `kind` order mutated; returns it + a record."""
    clone = copy.deepcopy(tree)
    # Re-find the same site in the clone by walking both trees in lockstep order.
    orig_nodes = list(ast.walk(tree))
    clone_nodes = list(ast.walk(clone))
    idx = next(i for i, node in enumerate(orig_nodes) if node is target)
    node = clone_nodes[idx]
    line = getattr(node, "lineno", 0)
    before = _src(node)
    if kind == "compare":
        node.ops = [_CMP.get(type(op), type(op))() for op in node.ops[:1]] + node.ops[1:]
    elif kind == "arith":
        node.op = _ARITH[type(node.op)]()
    elif kind == "boolop":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "not":
        _replace(clone, node, node.operand)
    elif kind == "const":
        node.value = (not node.value) if isinstance(node.value, bool) else node.value + 1
    elif kind == "cond":
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
    elif kind == "return":
        node.value = ast.Constant(value=None)
    ast.fix_missing_locations(clone)
    after = _src(node) if kind not in ("not",) else _src(node.operand)
    return clone, Mutant(n, line, kind, before, after)


def _replace(tree: ast.AST, old: ast.AST, new: ast.AST) -> None:
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if value is old:
                setattr(parent, field, new)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if item is old:
                        value[i] = new


def run(args: argparse.Namespace) -> int:
    path = Path(args.module)
    original = path.read_text()
    tree = ast.parse(original)
    scope = _restrict(tree, args.func)
    sites = _Sites()
    sites.visit(scope)
    todo = sites.sites[: args.max] if args.max else sites.sites
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    cmd = [sys.executable, "-m", "pytest", *args.tests, "-x", "-q", "-p", "no:cacheprovider",
           "-o", "addopts=", "--no-header", "-p", "no:randomly"]  # fmt: skip
    base = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=args.timeout)
    if base.returncode != 0:
        print(base.stdout[-1500:], base.stderr[-500:])
        raise SystemExit("the tests must pass on the unmutated module first")
    backup = path.with_suffix(path.suffix + ".mutbak")
    shutil.copy2(path, backup)
    results: list[Mutant] = []
    t0 = time.time()
    try:
        for n, (kind, target) in enumerate(todo, 1):
            mod_tree = tree
            clone, m = _mutate(mod_tree, target, kind, n)
            if m.original == m.mutated:
                continue
            path.write_text(ast.unparse(clone) + "\n")
            try:
                r = subprocess.run(cmd, env=env, capture_output=True, timeout=args.timeout)
                m.status = "killed" if r.returncode != 0 else "survived"
            except subprocess.TimeoutExpired:
                m.status = "timeout"  # an infinite loop is a detected mutant
            results.append(m)
            print(f"[{n}/{len(todo)}] L{m.line} {m.kind}: {m.status}", flush=True)
    finally:
        shutil.copy2(backup, path)
        backup.unlink(missing_ok=True)
    killed = sum(m.status in ("killed", "timeout") for m in results)
    total = len(results)
    out = {
        "module": args.module,
        "func": args.func,
        "tests": args.tests,
        "total": total,
        "killed": killed,
        "score": round(100 * killed / total, 1) if total else 100.0,
        "seconds": round(time.time() - t0),
        "survivors": [asdict(m) for m in results if m.status == "survived"],
    }
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1))
    print(f"\n{args.module}: killed {killed}/{total} = {out['score']}%")
    for m in results:
        if m.status == "survived":
            print(f"  SURVIVED L{m.line} [{m.kind}] {m.original}  ->  {m.mutated}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("module")
    ap.add_argument("tests", nargs="+")
    ap.add_argument("--func", help="only mutate this function or class")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--json")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
