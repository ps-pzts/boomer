"""One visual language for every Telegram message (#139): bold header + italic IST
subtitle, aligned rows in <pre>, status dots, <blockquote> hints, capped lists. Pure
functions, HTML only (Telegram's parse_mode=HTML). Callers escape dynamic text with esc()."""

from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LIMIT = 4000  # Telegram rejects > 4096; headroom for the truncation note
_TAG = re.compile(r"<(/?)(b|i|u|s|code|pre|blockquote)\b[^>]*>")


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def money(x: float | None) -> str:
    """Signed rupees with thousands separators: +₹1,250, -₹30, ₹0."""
    v = float(x or 0.0)
    if abs(v) < 0.5:
        return "₹0"
    return f"{'+' if v > 0 else '-'}₹{abs(v):,.0f}"


def dot(ok: bool) -> str:
    return "🟢" if ok else "🔴"


def stamp(mode: str | None = None, now: datetime | None = None) -> str:
    """'Sat 21 Nov · 13:03 IST' (+ ' · PAPER')."""
    t = (now or datetime.now(IST)).astimezone(IST)
    return t.strftime("%a %d %b · %H:%M IST") + (f" · {mode.upper()}" if mode else "")


def header(icon: str, title: str, sub: str | None = None) -> str:
    """Title and subtitle are plain text; they are escaped here."""
    return f"<b>{icon} {esc(title)}</b>" + (f"\n<i>{esc(sub)}</i>" if sub is not None else "")


def rows(pairs: list[tuple[str, object]]) -> str:
    """Key/value rows in a <pre>: keys left, values right-aligned (max ~34 wide)."""
    items = [(str(k), str(v)) for k, v in pairs]
    w = min(34, max(len(k) + 1 + len(v) for k, v in items))
    out = [k + " " * max(1, w - len(k) - len(v)) + v for k, v in items]
    return "<pre>" + esc("\n".join(out)) + "</pre>"


def note(markup: str) -> str:
    """A hint in a blockquote. `markup` is already escaped (may hold <code>/slash commands)."""
    return f"<blockquote>{markup}</blockquote>"


def capped(items: list[str], cap: int = 15) -> str:
    """One line per item, '+N more' past the cap."""
    shown = items[:cap]
    if len(items) > cap:
        shown.append(f"<i>+{len(items) - cap} more</i>")
    return "\n".join(shown)


def finish(text: str, limit: int = LIMIT) -> str:
    """Hard-cap a message for Telegram: cut at a line end, drop a half tag/entity, close
    any open tags, and say it was truncated."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut[: cut.rindex("\n")]
    cut = re.sub(r"(<[^>]*|&\w*)$", "", cut)
    stack: list[str] = []
    for m in _TAG.finditer(cut):
        if m.group(1):
            if m.group(2) in stack:
                del stack[stack.index(m.group(2)) :]
        else:
            stack.append(m.group(2))
    return cut + "".join(f"</{t}>" for t in reversed(stack)) + "\n<i>… truncated</i>"
