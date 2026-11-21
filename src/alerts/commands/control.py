"""/pause, /resume, /emergency: the bot mode, from the phone (#103). Each change goes
through BotModeStore, so it is logged in bot_mode_log with `telegram` as the actor."""

from __future__ import annotations

from alerts.commands.common import conn, session_row, today
from alerts.format import esc, note
from orchestrator.models import BotMode, BotModeStore


def _set(db_path: str, mode: BotMode, reason: str) -> None:
    BotModeStore(db_path).set_mode(mode, changed_by="telegram", reason=reason)


def pause(db_path: str, args: str = "") -> str:
    current = BotModeStore(db_path).current_mode()
    if current == BotMode.EMERGENCY_STOP:
        return "⛔ <b>Emergency stop is active</b>; the day is over.\n" + note(
            "/resume first (from tomorrow)."
        )
    _set(db_path, BotMode.PAUSED, "telegram /pause")
    return "🟡 <b>Paused</b>\n" + note(
        "The session halts: no new entries. Open trades stay managed (stops, targets, "
        "trailing). /resume to continue."
    )


def resume(db_path: str, args: str = "") -> str:
    c = conn(db_path)
    try:
        row = session_row(c, today())
    finally:
        c.close()
    previous = BotModeStore(db_path).current_mode()
    _set(db_path, BotMode.AUTO, "telegram /resume")
    out = "🟢 <b>Resumed</b> (mode auto)"
    if previous == BotMode.EMERGENCY_STOP and row is not None and row["state"] in (
        "squaring_off", "closed", "abandoned"
    ):  # fmt: skip
        out += "\n" + note(
            "Today's session already ended after the emergency stop; it trades again tomorrow."
        )
    elif row is not None and row["state"] == "halted":
        out += "\n" + note(f"Still halted ({esc(row['halt_reason'])}); see /halt.")
    return out


def emergency(db_path: str, args: str = "") -> str:
    if args.strip().lower() != "confirm":
        return "🔴 <b>EMERGENCY STOP</b>\n" + note(
            "Cancels every stop and target, exits every position at market (including ones "
            "the bot did not open) and ends the session for today.\n"
            "Send <code>/emergency confirm</code> to proceed."
        )
    _set(db_path, BotMode.EMERGENCY_STOP, "telegram /emergency confirm")
    return "🔴 <b>EMERGENCY STOP engaged</b>\n" + note(
        "Flattening on the next session tick (within a minute)."
    )
