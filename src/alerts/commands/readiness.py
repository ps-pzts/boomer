"""/readiness: the paper -> live criteria with their numbers (#119). Evidence only: read-only,
and it never switches the mode (that is /mode and /toggle)."""

from __future__ import annotations

from alerts.format import esc, header, note, stamp
from executor.live_readiness import Status, build_report

_DOT = {Status.PASS: "🟢", Status.FAIL: "🔴", Status.INSUFFICIENT_DATA: "🟡"}


def readiness(db_path: str, args: str = "") -> str:
    try:
        report = build_report(db_path)
    except Exception as exc:  # a missing/unmigrated db must not kill the bot
        return f"⛔ Readiness report unavailable: {esc(exc)}"
    lines = [header("🧪", "Paper → live readiness", stamp())]
    lines += [f"{_DOT[c.status]} <b>{esc(c.name)}</b>: {esc(c.summary)}" for c in report.criteria]
    lines.append("🟢 <b>All criteria met</b>" if report.ready else "🔴 <b>NOT ready</b>")
    lines.append(note("Evidence only; never enables live trading."))
    return "\n".join(lines)
