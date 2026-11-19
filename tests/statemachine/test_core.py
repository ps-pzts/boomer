"""Tests for statemachine/core.py — spec validation and atomic transitions (#56)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from statemachine import (
    IllegalTransition,
    MachineSpec,
    SpecError,
    Transition,
    record_created,
    transition,
)

EVENTS_SQL = (Path(__file__).parents[2] / "migrations" / "0012_events.sql").read_text()


def _t(src: set[str], target: str, trigger: str, guard=None) -> Transition:
    return Transition(frozenset(src), target, trigger, guard)


def _spec(**overrides) -> MachineSpec:
    kw = dict(
        name="door",
        table="doors",
        id_column="door_id",
        state_column="state",
        states=frozenset({"closed", "open", "locked", "gone"}),
        initial="closed",
        terminal=frozenset({"gone"}),
        transitions=(
            _t({"closed"}, "open", "open", guard=lambda ctx: ctx.get("blocked")),
            _t({"open"}, "closed", "close"),
            _t({"closed"}, "locked", "lock"),
            _t({"locked"}, "closed", "unlock"),
            _t({"closed", "locked"}, "gone", "remove"),
        ),
        touch_column="updated_at",
    )
    kw.update(overrides)
    return MachineSpec(**kw)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(EVENTS_SQL)
    c.execute("CREATE TABLE doors (door_id TEXT PRIMARY KEY, state TEXT, updated_at TEXT)")
    c.execute("INSERT INTO doors VALUES ('d1', 'closed', NULL)")
    c.commit()
    return c


def _state(conn) -> str:
    return conn.execute("SELECT state FROM doors WHERE door_id='d1'").fetchone()[0]


def _events(conn) -> list[tuple]:
    return conn.execute(
        "SELECT kind, name, source, entity_type, entity_id, symbol, payload FROM events"
        " ORDER BY event_id"
    ).fetchall()


class TestSpecValidation:
    def test_valid_spec(self):
        assert _spec().find("closed", "open").target == "open"

    def test_unknown_state(self):
        with pytest.raises(SpecError, match="unknown states"):
            _spec(transitions=(_t({"closed"}, "ajar", "nudge"),))

    def test_initial_must_be_a_state(self):
        with pytest.raises(SpecError, match="initial"):
            _spec(initial="ajar")

    def test_terminal_with_outgoing(self):
        with pytest.raises(SpecError, match="terminal 'gone' has outgoing"):
            _spec(transitions=(*_spec().transitions, _t({"gone"}, "closed", "rebuild")))

    def test_unreachable_state(self):
        with pytest.raises(SpecError, match="unreachable"):
            _spec(transitions=(_t({"closed"}, "gone", "remove"),))

    def test_dead_end_state(self):
        # 'open' can be entered but never leads to a terminal state
        with pytest.raises(SpecError, match="'open' can never reach"):
            _spec(
                transitions=(
                    _t({"closed"}, "open", "open"),
                    _t({"closed"}, "locked", "lock"),
                    _t({"locked"}, "gone", "remove"),
                )
            )

    def test_ambiguous_trigger(self):
        with pytest.raises(SpecError, match="ambiguous"):
            _spec(transitions=(*_spec().transitions, _t({"closed"}, "gone", "open")))

    def test_bad_sql_identifier(self):
        with pytest.raises(SpecError, match="identifier"):
            _spec(table="doors; DROP TABLE x")


class TestTransition:
    def test_changes_state_and_records_event_atomically(self, conn):
        r = transition(
            conn,
            _spec(),
            "d1",
            "open",
            rule="user_pushed",
            inputs={"force": 3},
            correlation={"symbol": "INFY"},
        )
        assert (r.state, r.changed, r.blocked_by) == ("open", True, None)
        assert _state(conn) == "open"
        assert conn.execute("SELECT updated_at FROM doors").fetchone()[0] is not None
        ((kind, name, source, etype, eid, symbol, payload),) = _events(conn)
        assert (kind, name, source, etype, eid, symbol) == (
            "state_transition",
            "open",
            "door",
            "door",
            "d1",
            "INFY",
        )
        p = json.loads(payload)
        assert (p["from_state"], p["to_state"], p["rule"]) == ("closed", "open", "user_pushed")
        assert p["inputs"] == {"force": 3}
        assert not conn.in_transaction  # committed

    def test_illegal_trigger_raises_and_changes_nothing(self, conn):
        with pytest.raises(IllegalTransition, match="no 'close' from 'closed'"):
            transition(conn, _spec(), "d1", "close", rule="x")
        assert _state(conn) == "closed"
        assert _events(conn) == []
        assert not conn.in_transaction

    def test_missing_entity(self, conn):
        with pytest.raises(IllegalTransition, match="not found"):
            transition(conn, _spec(), "nope", "open", rule="x")

    def test_guard_blocks_records_and_keeps_state(self, conn):
        r = transition(conn, _spec(), "d1", "open", rule="user_pushed", ctx={"blocked": "jammed"})
        assert (r.state, r.changed, r.blocked_by) == ("closed", False, "jammed")
        assert _state(conn) == "closed"
        ((kind, name, *_rest, payload),) = _events(conn)
        assert (kind, name) == ("transition_blocked", "open")
        assert json.loads(payload)["blocked_by"] == "jammed"

    def test_event_failure_rolls_back_state(self, conn):
        """State and event commit together or not at all."""
        conn.execute("DROP TABLE events")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            transition(conn, _spec(), "d1", "open", rule="x")
        assert _state(conn) == "closed"
        assert not conn.in_transaction

    def test_joins_callers_open_transaction_without_committing(self, conn):
        conn.execute("UPDATE doors SET updated_at='caller' WHERE door_id='d1'")
        assert conn.in_transaction
        transition(conn, _spec(), "d1", "lock", rule="x")
        assert conn.in_transaction  # caller still owns the commit
        conn.rollback()
        assert _state(conn) == "closed"  # rolled back together with the caller's write
        assert _events(conn) == []

    def test_terminal_state_has_no_way_out(self, conn):
        transition(conn, _spec(), "d1", "remove", rule="x")
        for trigger in ("open", "unlock", "remove"):
            with pytest.raises(IllegalTransition):
                transition(conn, _spec(), "d1", trigger, rule="x")

    def test_multi_source_transition(self, conn):
        transition(conn, _spec(), "d1", "lock", rule="x")
        assert transition(conn, _spec(), "d1", "remove", rule="x").state == "gone"


class TestRecordCreated:
    def test_birth_event_in_callers_transaction(self, conn):
        conn.execute("INSERT INTO doors VALUES ('d2', 'closed', NULL)")
        record_created(conn, _spec(), "d2", rule="installed", correlation={"symbol": "TCS"})
        conn.commit()
        (_kind, name, *_rest, payload) = _events(conn)[0]
        p = json.loads(payload)
        assert name == "created"
        assert (p["from_state"], p["to_state"], p["rule"]) == (None, "closed", "installed")
