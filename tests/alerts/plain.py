"""Tags stripped, entities decoded, whitespace collapsed: assert on numbers, not decoration."""

import html
import re


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def balanced(text: str) -> bool:
    """Every supported Telegram tag is closed in order."""
    stack: list[str] = []
    for m in re.finditer(r"<(/?)(b|i|u|s|code|pre|blockquote)\b[^>]*>", text):
        if not m.group(1):
            stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2):
            return False
    return not stack


HEADER = re.compile(r"^<b>\S+ [^\n]+</b>")


def is_card(text: str) -> bool:
    """Bold icon header, balanced tags, under Telegram's limit: fails on plain text."""
    return bool(HEADER.match(text)) and balanced(text) and len(text) < 4096
