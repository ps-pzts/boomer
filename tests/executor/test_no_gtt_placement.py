"""Guard (#129): the CNC GTT path is gone and cannot be re-wired into production.

GTT placement survives only where the backtester needs it (KiteBroker/KiteSim stand in
for Kite there); the production wiring may never define or call it."""

from __future__ import annotations

import ast
from pathlib import Path

from executor.brokers.base import Broker
from executor.position_manager import PositionManager

SRC = Path(__file__).resolve().parents[2] / "src"
# Backtester stack: BarBroker -> KiteBroker -> KiteSim. Nothing else may touch GTTs.
ALLOWED = {
    "backtester/bar_broker.py",
    "backtester/simulation.py",
    "executor/brokers/kite_broker.py",
    "executor/brokers/kite_gtt.py",
    "executor/brokers/kitesim/sim.py",
    "executor/brokers/kitesim/engine.py",
    "executor/brokers/kitesim/gtt.py",
}


def _gtt_placement_sites(tree: ast.AST) -> list[str]:
    """Names of functions defined, or attributes called, that place a GTT."""
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.endswith("_gtt"):
            hits.append(n.name)
        elif isinstance(n, ast.Attribute) and n.attr == "place_gtt":
            hits.append(n.attr)
    return hits


def test_only_the_backtester_stack_places_gtts():
    offenders = {}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        hits = _gtt_placement_sites(ast.parse(path.read_text()))
        if hits:
            offenders[rel] = hits
    assert offenders == {}


def test_the_scanner_flags_a_rewired_placement():
    tree = ast.parse("def place_gtt(self): pass\nbroker.place_gtt(req)")
    assert _gtt_placement_sites(tree) == ["place_gtt", "place_gtt"]


def test_gtt_manager_module_is_gone():
    assert not (SRC / "executor" / "gtt_manager.py").exists()


def test_broker_interface_and_position_manager_have_no_gtt_surface():
    assert not [m for m in dir(Broker) if "gtt" in m]
    assert not [m for m in dir(PositionManager) if "gtt" in m or m == "trail_stop"]
