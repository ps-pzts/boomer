"""Seeded random days and the event-log guarantees (#88).

 * invariants hold after every step of every seeded day
 * the same seed gives an identical event log (reproducible)
 * replaying state_transition events rebuilds every machine row's state
A failing seed is a reproduction: `play(Hostile(tmp_path), seed)`.
"""

from __future__ import annotations

import pytest

from tests.integration.harness import Hostile
from tests.integration.randomday import play
from tests.integration.replay import normalised_log, replay_mismatches

SEEDS = range(15)


@pytest.mark.parametrize("seed", SEEDS)
def test_invariants_hold_across_a_random_day(tmp_path, seed):
    play(Hostile(tmp_path, seed=seed), seed)


@pytest.mark.parametrize("seed", [3, 11, 19])
def test_same_seed_gives_an_identical_event_log(tmp_path, seed):
    runs = []
    for i in (1, 2):
        h = Hostile(tmp_path / f"run{i}", seed=seed)
        play(h, seed)
        runs.append(normalised_log(h.db))
    assert runs[0] == runs[1]
    assert len(runs[0]) > 20


@pytest.mark.parametrize("seed", [2, 5, 13])
def test_a_different_seed_gives_a_different_day(tmp_path, seed):
    logs = []
    for i, s in enumerate((seed, seed + 100)):
        h = Hostile(tmp_path / f"run{i}", seed=s)
        play(h, s)
        logs.append(normalised_log(h.db))
    assert logs[0] != logs[1]


@pytest.mark.parametrize("seed", SEEDS)
def test_replaying_events_rebuilds_every_row_state(tmp_path, seed):
    h = Hostile(tmp_path, seed=seed)
    play(h, seed)
    assert replay_mismatches(h.db) == []
