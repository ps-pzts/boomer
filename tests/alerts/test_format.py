"""The shared Telegram format helpers (#139).

Worked example: Realised +₹1,127.00 and Unrealised -₹2,500.50 in one rows() block end on
the same column, so the numbers line up on a phone."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alerts import format as fmt
from alerts import telegram, telegram_bot
from tests.alerts.plain import balanced

WHEN = datetime(2026, 11, 21, 13, 3, tzinfo=fmt.IST)


def test_money_is_signed_with_thousands_separators():
    assert [fmt.money(x) for x in (1127, -2500.5, 0.2, None)] == ["+₹1,127", "-₹2,500", "₹0", "₹0"]


def test_header_and_stamp_are_ist():
    assert fmt.stamp("paper", WHEN) == "Sat 21 Nov · 13:03 IST · PAPER"
    utc = datetime(2026, 11, 21, 7, 33, tzinfo=ZoneInfo("UTC"))
    assert fmt.stamp(now=utc) == "Sat 21 Nov · 13:03 IST"
    assert fmt.header("📊", "Boomer", "sub") == "<b>📊 Boomer</b>\n<i>sub</i>"


def test_rows_align_numbers_on_the_right_edge():
    block = fmt.rows([("Realised", "+₹1,127.00"), ("Unrealised", "-₹2,500.50")])
    lines = block.removeprefix("<pre>").removesuffix("</pre>").split("\n")
    assert lines == ["Realised   +₹1,127.00", "Unrealised -₹2,500.50"]
    assert len({len(line) for line in lines}) == 1 and max(map(len, lines)) <= 34


def test_dynamic_text_is_escaped_everywhere():
    assert fmt.esc("M&M <b>") == "M&amp;M &lt;b&gt;"
    assert "M&amp;M" in fmt.rows([("Symbol", "M&M")])
    assert fmt.header("📥", "<x> · M&M") == "<b>📥 &lt;x&gt; · M&amp;M</b>"


def test_capped_lists_say_how_many_are_hidden():
    assert fmt.capped(["a", "b", "c"], 5) == "a\nb\nc"
    assert fmt.capped(["a", "b", "c"], 2) == "a\nb\n<i>+1 more</i>"


def test_finish_keeps_short_text_and_caps_a_pathological_list():
    assert fmt.finish("<b>ok</b>") == "<b>ok</b>"
    items = [f"🟢 <b>SYM{i}</b> 10 @ 100.00→101.50 +₹15 · 🛑 98.00 🎯 104.00" for i in range(500)]
    out = fmt.finish("<blockquote><pre>" + "\n".join(items))
    assert len(out) <= 4096 and out.endswith("… truncated</i>")
    assert balanced(out) and "SYM0" in out and "SYM499" not in out


def test_finish_never_leaves_half_a_tag_or_entity():
    for tail in ("<b", "&am", "x</b"):
        out = fmt.finish("<b>" + "a" * 10 + tail, limit=14)
        assert balanced(out) and "&am" not in out and "<b<" not in out


def test_send_telegram_caps_the_payload():
    long = "<b>" + "x\n" * 5000
    with patch("alerts.telegram.urllib.request.urlopen") as op:
        op.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'
        assert telegram.send_telegram("t", "c", long)
    sent = json.loads(op.call_args.args[0].data)["text"]
    assert len(sent) <= 4096 and balanced(sent)


def test_bot_send_caps_the_payload():
    with patch.object(telegram_bot, "_call") as call:
        telegram_bot._send("t", 1, "<b>" + "x\n" * 5000)
    sent = call.call_args.args[2]["text"]
    assert len(sent) <= 4096 and balanced(sent)
