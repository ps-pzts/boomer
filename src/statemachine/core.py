"""State-machine core shared by the Session, Trade and Order machines (#56).

designs/state-machines.md §3. In-house and small on purpose: a library keeps state
in memory, but here the state change and its event must commit in ONE SQLite
transaction (restart-safe, and the event log can never disagree with the state
tables).

Each machine keeps its state in its own table. `transition()`:
  1. reads the entity's current state (inside the transaction)
  2. finds the transition for (state, trigger); none → IllegalTransition
  3. evaluates the guard (a pure function of `ctx`); blocked → records a
     `transition_blocked` event and returns the unchanged state
  4. updates the state column (and the optional touch column)
  5. records a `state_transition` event (observability.emit envelope)
  6. commits, unless the caller already had a transaction open (then the
     caller owns the commit and the transition joins it)
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from observability import emit as obs

IST = ZoneInfo("Asia/Kolkata")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

Guard = Callable[[Mapping[str, Any]], str | None]  # None = allowed, str = blocked reason


class IllegalTransition(Exception):
    """A trigger with no transition from the entity's current state."""


class SpecError(Exception):
    """A machine spec that is internally inconsistent (raised at import time)."""


@dataclass(frozen=True)
class Transition:
    source: frozenset[str]
    target: str
    trigger: str
    guard: Guard | None = None


@dataclass(frozen=True)
class Result:
    state: str
    changed: bool
    blocked_by: str | None = None


@dataclass(frozen=True)
class MachineSpec:
    name: str  # "session" | "trade" | "order"; also the event source
    table: str
    id_column: str
    state_column: str
    states: frozenset[str]
    initial: str
    terminal: frozenset[str]  # states with no outgoing transitions
    transitions: tuple[Transition, ...]
    touch_column: str | None = None  # e.g. updated_at, set to now on every change
    _index: dict[tuple[str, str], Transition] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._validate()
        for t in self.transitions:
            for s in t.source:
                self._index[(s, t.trigger)] = t

    def find(self, state: str, trigger: str) -> Transition | None:
        return self._index.get((state, trigger))

    def _validate(self) -> None:
        for ident in (self.table, self.id_column, self.state_column, self.touch_column):
            if ident is not None and not _IDENT.match(ident):
                raise SpecError(f"{self.name}: bad SQL identifier {ident!r}")
        if self.initial not in self.states:
            raise SpecError(f"{self.name}: initial {self.initial!r} not a state")
        if not self.terminal <= self.states:
            raise SpecError(f"{self.name}: terminal states not all in states")
        seen: set[tuple[str, str]] = set()
        outgoing: dict[str, set[str]] = {s: set() for s in self.states}
        for t in self.transitions:
            unknown = (t.source | {t.target}) - self.states
            if unknown:
                raise SpecError(f"{self.name}: unknown states {sorted(unknown)} in {t.trigger}")
            for s in t.source:
                if s in self.terminal:
                    raise SpecError(f"{self.name}: terminal {s!r} has outgoing {t.trigger}")
                if (s, t.trigger) in seen:
                    raise SpecError(f"{self.name}: ambiguous ({s}, {t.trigger})")
                seen.add((s, t.trigger))
                outgoing[s].add(t.target)
        reachable = _closure({self.initial}, outgoing)
        if reachable != set(self.states):
            raise SpecError(f"{self.name}: unreachable states {sorted(self.states - reachable)}")
        for s in self.states - self.terminal:
            if not (_closure({s}, outgoing) & self.terminal):
                raise SpecError(f"{self.name}: {s!r} can never reach a terminal state")


def _closure(start: set[str], outgoing: Mapping[str, set[str]]) -> set[str]:
    seen, stack = set(start), list(start)
    while stack:
        for nxt in outgoing.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _now() -> datetime:
    return datetime.now(IST)


def transition(
    conn: sqlite3.Connection,
    spec: MachineSpec,
    entity_id: str,
    trigger: str,
    *,
    rule: str,
    inputs: Mapping[str, Any] | None = None,
    correlation: Mapping[str, str | None] | None = None,
    ctx: Mapping[str, Any] | None = None,
    severity: str = "info",
) -> Result:
    """Apply `trigger` to the entity atomically with its event. See module doc."""
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"SELECT {spec.state_column} FROM {spec.table} WHERE {spec.id_column} = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            raise IllegalTransition(f"{spec.name} {entity_id} not found")
        current = row[0]
        t = spec.find(current, trigger)
        if t is None:
            raise IllegalTransition(f"{spec.name} {entity_id}: no '{trigger}' from {current!r}")
        blocked = t.guard(ctx or {}) if t.guard else None
        corr = dict(correlation or {})
        if blocked:
            _record(
                conn,
                spec,
                entity_id,
                "transition_blocked",
                trigger,
                corr,
                "info",
                state=current,
                target=t.target,
                blocked_by=blocked,
                rule=rule,
                inputs=dict(inputs or {}),
            )
            result = Result(current, changed=False, blocked_by=blocked)
        else:
            sets = f"{spec.state_column} = ?"
            params: list[Any] = [t.target]
            if spec.touch_column:
                sets += f", {spec.touch_column} = ?"
                params.append(_now().replace(tzinfo=None).isoformat())
            conn.execute(
                f"UPDATE {spec.table} SET {sets} WHERE {spec.id_column} = ?",
                (*params, entity_id),
            )
            _record(
                conn,
                spec,
                entity_id,
                "state_transition",
                trigger,
                corr,
                severity,
                machine=spec.name,
                from_state=current,
                to_state=t.target,
                rule=rule,
                inputs=dict(inputs or {}),
            )
            result = Result(t.target, changed=True)
        if own_txn:
            conn.commit()
        return result
    except BaseException:
        if own_txn:
            conn.rollback()
        raise


def record_created(
    conn: sqlite3.Connection,
    spec: MachineSpec,
    entity_id: str,
    *,
    rule: str,
    inputs: Mapping[str, Any] | None = None,
    correlation: Mapping[str, str | None] | None = None,
) -> None:
    """Record an entity's birth in `spec.initial`. Call in the same transaction as the
    INSERT of its row; the caller commits."""
    _record(
        conn,
        spec,
        entity_id,
        "state_transition",
        "created",
        dict(correlation or {}),
        "info",
        machine=spec.name,
        from_state=None,
        to_state=spec.initial,
        rule=rule,
        inputs=dict(inputs or {}),
    )


def _record(conn, spec, entity_id, kind, trigger, corr, severity, **payload) -> None:
    row = obs.get().envelope(
        kind,
        trigger,
        source=spec.name,
        severity=severity,
        entity_type=spec.name,
        entity_id=entity_id,
        cycle_id=corr.get("cycle_id"),
        trade_id=corr.get("trade_id"),
        order_id=corr.get("order_id"),
        symbol=corr.get("symbol"),
        **payload,
    )
    obs.Emitter.insert(conn, row)
