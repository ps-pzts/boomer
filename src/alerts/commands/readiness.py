"""/readiness: the paper -> live criteria with their numbers (#119). Evidence only: read-only,
and it never switches the mode (that is /mode and /toggle)."""

from __future__ import annotations

from alerts.commands.common import esc
from executor.live_readiness import build_report, render


def readiness(db_path: str, args: str = "") -> str:
    try:
        return f"<pre>{esc(render(build_report(db_path)))}</pre>"
    except Exception as exc:  # a missing/unmigrated db must not kill the bot
        return f"Readiness report unavailable: {esc(exc)}"
