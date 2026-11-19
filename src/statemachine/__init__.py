"""Shared state-machine core (designs/state-machines.md §3)."""

from statemachine.core import (
    IllegalTransition,
    MachineSpec,
    Result,
    SpecError,
    Transition,
    record_created,
    transition,
)

__all__ = [
    "IllegalTransition",
    "MachineSpec",
    "Result",
    "SpecError",
    "Transition",
    "record_created",
    "transition",
]
