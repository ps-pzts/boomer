"""Paper -> live readiness report (#119; design intraday-live-pipeline.md section 9 Q5).

EVIDENCE ONLY. Read-only: the database is opened `mode=ro`, nothing is written, and nothing
here can enable live trading (that stays with `executor.trading_mode`, the operator toggle
and the server arm). Only paper-mode sessions count; live sessions are ignored.

Each criterion reports PASS, FAIL or INSUFFICIENT_DATA with the exact numbers:

1. Sessions: closed paper sessions >= MIN_PAPER_SESSIONS (20).
2. Protection: zero unprotected moments across paper trading. A moment is any
   paper trade forced out by a missing stop (`exit_reason` `stop_<status>` other than
   `stop_hit`, which is how the Trade machine records its `stop_failed` / `stop_lost`
   transitions), any paper position still flagged unprotected, or any bracket in `open`
   with no stop order. State tables only: the events stream is read only by
   src/observability (tests/test_events_read_only.py).
   Zero sessions -> INSUFFICIENT_DATA; any moment -> FAIL.
3. Slippage: for every filled paper leg that has a stored reference price,
       adverse_bps = side_sign * (fill - ref) / ref * 10_000     (BUY +1, SELL -1)
   so a worse-than-reference fill is positive. Per leg class the mean must not exceed the
   backtester's assumption (`backtester.slippage.SlippageModel`): limit legs 0 bps
   (fill at the limit or better), stop legs BASE_SLIPPAGE_PCT * STOP_SLIPPAGE_FACTOR
   (5 bps * 1.5 = 7.5 bps). Market exits store no reference price and are counted, not
   judged. Reference: entry/target = limit price; stop = trigger price.
4. Expectancy: over closed paper trades (long only, MIS),
       net_pnl = realised_pnl - round_trip_cost(buy_value, sell_value)
       buy_value = filled_qty * entry fill price, sell_value = buy_value + realised_pnl
       expectancy = mean(net_pnl) per trade, must be > 0.
   Costs come from `backtester.costs.CostModel`. No closed trades -> INSUFFICIENT_DATA.
5. B4 (SEBI): a manual flag, env `BOOMER_B4_RESOLVED` truthy. Unset -> INSUFFICIENT_DATA.

Usage: python -m executor.live_readiness [--db PATH]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from backtester.costs import CostModel
from backtester.slippage import SlippageModel
from executor.models import ProductType

MIN_PAPER_SESSIONS = 20
B4_ENV_VAR = "BOOMER_B4_RESOLVED"
_TRUTHY = {"1", "true", "yes", "y", "resolved"}


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class Criterion:
    name: str
    status: Status
    summary: str
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    criteria: list[Criterion]

    @property
    def ready(self) -> bool:
        return all(c.status == Status.PASS for c in self.criteria)


def slippage_assumptions_bps() -> dict[str, float]:
    """The backtester's assumed adverse slippage per leg class, in bps."""
    return {
        "limit": 0.0,
        "stop": SlippageModel.BASE_SLIPPAGE_PCT * SlippageModel.STOP_SLIPPAGE_FACTOR * 10_000,
    }


def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _paper_dates(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute("SELECT session_date FROM trading_sessions WHERE mode='paper'")]


# ── 1. sessions ───────────────────────────────────────────────────────────────


def check_sessions(c: sqlite3.Connection) -> Criterion:
    n = c.execute(
        "SELECT COUNT(*) FROM trading_sessions WHERE mode='paper' AND state='closed'"
    ).fetchone()[0]
    ok = n >= MIN_PAPER_SESSIONS
    return Criterion(
        "paper_sessions",
        Status.PASS if ok else Status.INSUFFICIENT_DATA,
        f"{n} closed paper sessions (need >= {MIN_PAPER_SESSIONS})",
        {"closed_paper_sessions": n, "required": MIN_PAPER_SESSIONS},
    )


# ── 2. protection ─────────────────────────────────────────────────────────────


def check_protection(c: sqlite3.Connection) -> Criterion:
    dates = _paper_dates(c)
    if not dates:
        return Criterion("zero_unprotected", Status.INSUFFICIENT_DATA, "no paper sessions yet")
    marks = ",".join("?" * len(dates))
    forced = c.execute(
        "SELECT COUNT(*) FROM intraday_brackets WHERE exit_reason LIKE 'stop\\_%' ESCAPE '\\'"
        f" AND exit_reason != 'stop_hit' AND session_date IN ({marks})",
        dates,
    ).fetchone()[0]
    flagged = c.execute(
        "SELECT COUNT(*) FROM positions WHERE unprotected_flag=1"
        f" AND substr(entry_at,1,10) IN ({marks})",
        dates,
    ).fetchone()[0]
    no_stop = c.execute(
        f"SELECT COUNT(*) FROM intraday_brackets WHERE state='open' AND stop_order_id IS NULL"
        f" AND session_date IN ({marks})",
        dates,
    ).fetchone()[0]
    total = forced + flagged + no_stop
    return Criterion(
        "zero_unprotected",
        Status.PASS if total == 0 else Status.FAIL,
        f"{total} unprotected moments (need 0): {forced} trades forced out by a missing stop, "
        f"{flagged} positions flagged, {no_stop} open trades without a stop",
        {"forced_exits": forced, "positions_flagged": flagged, "open_without_stop": no_stop},
    )


# ── 3. slippage ───────────────────────────────────────────────────────────────

_LEG_SQL = """
SELECT o.side, o.order_type, o.price, o.trigger_price, o.average_fill_price, o.filled_quantity
FROM intraday_brackets b JOIN orders o ON o.order_id = b.{col}
WHERE b.session_date IN ({marks}) AND o.filled_quantity > 0
"""


def adverse_bps(side: str, fill: float, ref: float) -> float:
    """side_sign * (fill - ref) / ref * 10_000; positive = worse than the reference.

    Worked example: BUY limit 500.00 filled 500.25 -> +5.0 bps; SELL stop trigger 200.00
    filled 199.70 -> -(199.70-200)/200*10_000 = +15.0 bps (a gap through the stop).
    """
    sign = 1 if side == "buy" else -1
    return sign * (fill - ref) / ref * 10_000


def _leg_class(order_type: str) -> str | None:
    if order_type == "limit":
        return "limit"
    if order_type in ("sl", "sl_limit", "sl-m", "sl_m"):
        return "stop"
    return None


def check_slippage(c: sqlite3.Connection) -> Criterion:
    dates = _paper_dates(c)
    assumed = slippage_assumptions_bps()
    samples: dict[str, list[float]] = {"limit": [], "stop": []}
    unjudged = 0
    if dates:
        marks = ",".join("?" * len(dates))
        for col in ("entry_order_id", "stop_order_id", "target_order_id"):
            for r in c.execute(_LEG_SQL.format(col=col, marks=marks), dates):
                cls = _leg_class(r["order_type"])
                ref = r["trigger_price"] if cls == "stop" else r["price"]
                if cls is None or not ref or ref <= 0:
                    unjudged += 1
                    continue
                samples[cls].append(adverse_bps(r["side"], r["average_fill_price"], ref))
        # Market exits (corrective / square-off) carry no reference price: count only.
        for (ids,) in c.execute(
            f"SELECT exit_order_ids FROM intraday_brackets WHERE session_date IN ({marks})", dates
        ):
            unjudged += ids.count('"') // 2
    means = {k: sum(v) / len(v) for k, v in samples.items() if v}
    details = {
        "assumed_bps": assumed,
        "mean_adverse_bps": {k: round(v, 2) for k, v in means.items()},
        "samples": {k: len(v) for k, v in samples.items()},
        "market_or_unreferenced_legs": unjudged,
    }
    if not means:
        return Criterion(
            "slippage", Status.INSUFFICIENT_DATA, "no filled paper legs with a reference", details
        )
    over = [k for k, m in means.items() if m > assumed[k] + 1e-9]
    parts = ", ".join(f"{k} {means[k]:+.2f} bps (assumed {assumed[k]:.2f})" for k in means)
    return Criterion(
        "slippage",
        Status.FAIL if over else Status.PASS,
        f"mean adverse slippage: {parts}",
        details,
    )


# ── 4. expectancy ─────────────────────────────────────────────────────────────


def check_expectancy(c: sqlite3.Connection) -> Criterion:
    dates = _paper_dates(c)
    nets: list[float] = []
    gross = costs = 0.0
    if dates:
        marks = ",".join("?" * len(dates))
        model = CostModel()
        rows = c.execute(
            "SELECT b.realised_pnl, b.filled_quantity, o.average_fill_price AS entry_px"
            " FROM intraday_brackets b JOIN orders o ON o.order_id = b.entry_order_id"
            f" WHERE b.state='closed' AND b.session_date IN ({marks})"
            " AND b.filled_quantity > 0 AND b.realised_pnl IS NOT NULL",
            dates,
        ).fetchall()
        for r in rows:
            buy = r["filled_quantity"] * r["entry_px"]
            cost = model.round_trip_cost(buy, buy + r["realised_pnl"], ProductType.MIS).total
            gross += r["realised_pnl"]
            costs += cost
            nets.append(r["realised_pnl"] - cost)
    n = len(nets)
    if n == 0:
        return Criterion("expectancy", Status.INSUFFICIENT_DATA, "no closed paper trades")
    exp = sum(nets) / n
    wins = sum(1 for x in nets if x > 0)
    return Criterion(
        "expectancy",
        Status.PASS if exp > 0 else Status.FAIL,
        f"{exp:+,.2f} per trade after costs over {n} trades (need > 0); win rate {wins / n:.0%}",
        {
            "trades": n,
            "gross_pnl": round(gross, 2),
            "costs": round(costs, 2),
            "net_pnl": round(gross - costs, 2),
            "expectancy_per_trade": round(exp, 4),
            "win_rate": round(wins / n, 4),
        },
    )


# ── 5. B4 ─────────────────────────────────────────────────────────────────────


def check_b4(env_value: str | None) -> Criterion:
    if (env_value or "").strip().lower() in _TRUTHY:
        return Criterion("b4_sebi", Status.PASS, f"{B4_ENV_VAR} set: operator asserts B4 resolved")
    return Criterion(
        "b4_sebi",
        Status.INSUFFICIENT_DATA,
        f"manual flag {B4_ENV_VAR} not set (operator must assert B4 is resolved)",
    )


# ── report ────────────────────────────────────────────────────────────────────


def build_report(db_path: str | Path, b4_env: str | None = None) -> Report:
    c = _connect_ro(db_path)
    try:
        return Report(
            [
                check_sessions(c),
                check_protection(c),
                check_slippage(c),
                check_expectancy(c),
                check_b4(os.environ.get(B4_ENV_VAR) if b4_env is None else b4_env),
            ]
        )
    finally:
        c.close()


_ICON = {Status.PASS: "PASS", Status.FAIL: "FAIL", Status.INSUFFICIENT_DATA: "NO DATA"}


def render(report: Report) -> str:
    lines = ["Paper -> live readiness (evidence only; never enables live trading)"]
    for cr in report.criteria:
        lines.append(f"[{_ICON[cr.status]}] {cr.name}: {cr.summary}")
    lines.append("Verdict: " + ("all criteria met" if report.ready else "NOT ready"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Paper -> live readiness report (read-only).")
    p.add_argument("--db", default=os.environ.get("BOOMER_DB_PATH", "data/boomer.db"))
    args = p.parse_args(argv)
    print(render(build_report(args.db)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
