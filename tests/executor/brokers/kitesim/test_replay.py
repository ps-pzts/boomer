"""Conformance (b): real Kite recordings keep validating KiteSim (#86).

The 09:15 canary (#85) records every raw Kite response to
data/kite_recordings/YYYY-MM-DD/canary-HHMMSS.json. Replaying the same calls
(prices from its ltp reads, then place / modify / cancel) into KiteSim must give
the same order statuses at every orders() read. Kite's transient in-flight
statuses (MODIFY PENDING, ...) are skipped: KiteSim settles each call at once.
Skipped when no recording exists yet; a recording made against KiteSim checks
the replay machinery itself.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from executor.brokers.kite_contract import IN_FLIGHT_ORDER_STATUSES
from executor.brokers.kitesim import InstrumentBook, KiteSim, KiteSimConfig
from executor.canary import CanaryConfig, run_canary
from tests.kitesim_kit import IST, Clock, live_shaped_broker, make_sim

RECORDINGS = Path(__file__).parents[4] / "data" / "kite_recordings"


def _lenient_instruments() -> InstrumentBook:
    """Real prices sit on the real dump's ticks; any 2-decimal price is on 0.01."""

    def loader(exchange: str) -> list[dict]:
        return []

    book = InstrumentBook(loader, synthesize_unknown=True)
    book.row = lambda symbol, exchange: {  # type: ignore[method-assign]
        "instrument_token": 1,
        "tradingsymbol": symbol,
        "exchange": exchange,
        "tick_size": 0.01,
    }
    return book


def replay(path: Path) -> list[tuple[str, str, str]]:
    """(recorded order id, recorded status, simulated status) at each orders() read."""
    body = json.loads(path.read_text())
    clock = Clock()
    sim = KiteSim(_lenient_instruments(), 1e9, clock, KiteSimConfig())
    ids: dict[str, str] = {}
    seen: list[tuple[str, str, str]] = []
    for r in body["responses"]:
        clock.now = datetime.fromisoformat(r["at"]).replace(tzinfo=IST)
        call, params, resp = r["call"], dict(r["params"]), r["response"]
        if call == "ltp":
            for key, quote in (resp or {}).items():
                exchange, _, symbol = key.partition(":")
                sim.on_price(symbol, exchange, float(quote["last_price"]))
        elif call == "place_order":
            ids[str(resp)] = sim.place_order(variety="regular", **params)
        elif call in ("modify_order", "cancel_order"):
            params["order_id"] = ids[str(params["order_id"])]
            getattr(sim, call)(variety="regular", **params)
        elif call == "orders":
            mine = {o["order_id"]: o["status"] for o in sim.orders()}
            for o in resp or []:
                rid = str(o["order_id"])
                if rid in ids and o["status"] not in IN_FLIGHT_ORDER_STATUSES:
                    seen.append((rid, o["status"], mine[ids[rid]]))
    return seen


def _recordings() -> list[Path]:
    return sorted(RECORDINGS.glob("*/canary-*.json")) if RECORDINGS.exists() else []


@pytest.mark.parametrize("path", _recordings() or [None], ids=lambda p: p.name if p else "none")
def test_kitesim_matches_every_real_recording(path):
    if path is None:
        pytest.skip("no canary recordings in data/kite_recordings yet")
    seen = replay(path)
    assert seen, f"{path} has no orders() reads of the canary's orders"
    assert [(r, kite) for r, kite, _ in seen] == [(r, sim) for r, _, sim in seen]


def test_replay_of_a_canary_run_against_kitesim(tmp_path):
    sim = make_sim(clock=Clock(datetime(2026, 11, 19, 9, 15, 5, tzinfo=IST)))
    sim.on_price("INFY", "NSE", 1503.4)
    cfg = CanaryConfig(recordings_dir=str(tmp_path))
    report = run_canary(live_shaped_broker(sim), cfg, clock=sim._clock, sleep=lambda s: None)
    assert report.ok, report.failures()
    seen = replay(Path(report.extra["recording"]))
    statuses = [kite for _, kite, _ in seen]
    assert {"TRIGGER PENDING", "OPEN", "CANCELLED"} <= set(statuses)
    assert all(kite == simulated for _, kite, simulated in seen)
