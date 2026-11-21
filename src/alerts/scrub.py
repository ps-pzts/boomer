"""Secret scrubber (#142): one place that removes secret values from any text that leaves
the process (Telegram, logs). Exact env-var values are looked up at call time, so a token
rotated into os.environ mid-run is covered; the regexes catch secrets we do not hold."""

from __future__ import annotations

import html
import logging
import os
import re
import stat
from pathlib import Path

REDACTED = "‹redacted›"
_SECRET_NAME = re.compile(r"(_SECRET|_PASSWORD|_TOKEN|_TOTP_SECRET|_API_KEY|_PASS)$")
_PATTERNS = [
    re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"((?:request_token|access_token|api_secret|checksum)=)[^&\s\"']+"),
]
_MIN_LEN = 6  # shorter values would shred ordinary words


def scrub(text: object) -> str:
    out = str(text)
    values = {v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v) >= _MIN_LEN}
    # the HTML-escaped form too: esc() may run before scrub (a secret holding & < >)
    values |= {html.escape(v, quote=False) for v in values}
    for v in sorted(values, key=len, reverse=True):
        out = out.replace(v, REDACTED)
    out = _PATTERNS[0].sub(REDACTED, out)
    return _PATTERNS[1].sub(lambda m: m.group(1) + REDACTED, out)


class ScrubFilter(logging.Filter):
    """Scrubs message, args-formatted text, exception text and stack text of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        if record.exc_info:  # Formatter reuses exc_text when set
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def install_log_scrubber() -> None:
    """Call right after logging is configured, in every process entry point."""
    for h in logging.getLogger().handlers:
        if not any(isinstance(f, ScrubFilter) for f in h.filters):
            h.addFilter(ScrubFilter())


def warn_if_loose_perms(*paths: str | Path, mask: int = 0o077) -> None:
    """Log one WARNING per existing path whose mode has any `mask` bit. Never raises.
    Files use the default 0o077; directories pass 0o022 so a normal 755 stays quiet."""
    log = logging.getLogger(__name__)
    for p in paths:
        try:
            mode = stat.S_IMODE(Path(p).stat().st_mode)
        except OSError:
            continue
        if mode & mask:
            log.warning("loose_permissions path=%s mode=%o fix=chmod_go-rwx", p, mode)
