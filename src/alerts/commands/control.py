"""/pause, /resume, /emergency: the bot mode, from the phone (#103). Each change goes
through BotModeStore, so it is logged in bot_mode_log with `telegram` as the actor."""

from __future__ import annotations

from src.alerts.commands.common import conn, session_row, today
from src.orchestrator.models import BotMode, BotModeStore


def _set(db_path: str, mode: BotMode, reason: str) -> None:
    BotModeStore(db_path).set_mode(mode, changed_by="telegram", reason=reason)


def pause(db_path: str, args: str = "") -> str:
    current = BotModeStore(db_path).current_mode()
    if current == BotMode.EMERGENCY_STOP:
        return "⛔ Emergency stop is active; the day is over. /resume first (from tomorrow)."
    _set(db_path, BotMode.PAUSED, "telegram /pause")
    return (
        "🟡 <b>Paused.</b> The session halts: no new entries. Open trades stay managed "
        "(stops, targets, trailing). /resume to continue."
    )


def resume(db_path: str, args: str = "") -> str:
    c = conn(db_path)
    try:
        row = session_row(c, today())
    finally:
        c.close()
    previous = BotModeStore(db_path).current_mode()
    _set(db_path, BotMode.AUTO, "telegram /resume")
    note = ""
    if previous == BotMode.EMERGENCY_STOP and row is not None and row["state"] in (
        "squaring_off", "closed", "abandoned"
    ):  # fmt: skip
        note = "\nToday's session already ended after the emergency stop; it trades again tomorrow."
    elif row is not None and row["state"] == "halted":
        note = f"\nStill halted ({row['halt_reason']}); see /halt."
    return "🟢 <b>Resumed</b> (mode auto)." + note


def emergency(db_path: str, args: str = "") -> str:
    if args.strip().lower() != "confirm":
        return (
            "🔴 <b>EMERGENCY STOP</b> cancels every stop and target, exits every position at "
            "market (including ones the bot did not open) and ends the session for today.\n"
            "Send <code>/emergency confirm</code> to proceed."
        )
    _set(db_path, BotMode.EMERGENCY_STOP, "telegram /emergency confirm")
    return (
        "🔴 <b>EMERGENCY STOP engaged.</b> Flattening on the next session tick (within a minute)."
    )
