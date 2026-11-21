"""alerts.scrub (#142). Every secret here is a fake."""

from __future__ import annotations

import io
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from alerts.format import esc, finish
from alerts.scrub import REDACTED, ScrubFilter, install_log_scrubber, scrub, warn_if_loose_perms
from alerts.telegram import send_telegram

FAKE_BOT = "1234567890:AAFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE_x-y"
FAKE_ACCESS = "fakeaccesstoken0123456789"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in list(os.environ):
        if k.endswith(("_SECRET", "_PASSWORD", "_TOKEN", "_API_KEY")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("KITE_ACCESS_TOKEN", FAKE_ACCESS)
    monkeypatch.setenv("KITE_PASSWORD", "fakepass-9")
    monkeypatch.setenv("KITE_TOTP_SECRET", "SHORT")  # < 6 chars: ignored
    monkeypatch.setenv("CUSTOM_THING_SECRET", "customvalue42")


def test_env_values_are_redacted_at_call_time(monkeypatch):
    assert scrub(f"tok {FAKE_ACCESS} / fakepass-9 / customvalue42") == (
        f"tok {REDACTED} / {REDACTED} / {REDACTED}"
    )
    monkeypatch.setenv("KITE_API_SECRET", "rotatedlater77")  # set after import
    assert scrub("x rotatedlater77") == f"x {REDACTED}"


def test_short_values_ignored():
    assert scrub("the SHORT word") == "the SHORT word"


def test_request_url_query_secrets_redacted():
    url = "https://kite.trade/connect/login?api_key=k&request_token=abc123XYZ&checksum=deadbeef"
    out = scrub(f"HTTPError for url: {url}")
    assert "abc123XYZ" not in out
    assert "deadbeef" not in out
    assert f"request_token={REDACTED}&checksum={REDACTED}" in out


def test_telegram_bot_token_shape_redacted():
    out = scrub(f"POST https://api.telegram.org/bot{FAKE_BOT}/sendMessage failed")
    assert FAKE_BOT not in out
    assert "AAFAKE" not in out


def test_idempotent_and_plain_text_untouched():
    once = scrub(f"a {FAKE_ACCESS} b request_token=zzz")
    assert scrub(once) == once
    assert scrub("nothing secret: 2026-11-21 ₹1,250") == "nothing secret: 2026-11-21 ₹1,250"


def test_finish_scrubs_even_short_messages():
    assert FAKE_ACCESS not in finish(f"<b>{FAKE_ACCESS}</b>")


def test_send_telegram_payload_is_scrubbed():
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = b'{"ok": true}'
    with patch("alerts.telegram.urllib.request.urlopen", return_value=resp) as up:
        assert send_telegram("tok", "1", f"leak {FAKE_ACCESS}")
    assert FAKE_ACCESS.encode() not in up.call_args[0][0].data


def _logger_with_filter():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(ScrubFilter())
    log = logging.Logger("t")
    log.addHandler(h)
    return log, buf


def test_filter_masks_msg_args_and_exception_text():
    log, buf = _logger_with_filter()
    log.error("direct %s", FAKE_ACCESS)
    log.error(f"msg {FAKE_ACCESS}")
    try:
        raise RuntimeError(f"boom request_token=abc999 {FAKE_ACCESS}")
    except RuntimeError:
        log.exception("failed")
    out = buf.getvalue()
    assert FAKE_ACCESS not in out
    assert "abc999" not in out
    assert out.count(REDACTED) >= 4


def test_install_adds_filter_once_to_root_handlers():
    root = logging.getLogger()
    h = logging.StreamHandler(io.StringIO())
    root.addHandler(h)
    try:
        install_log_scrubber()
        install_log_scrubber()
        assert [type(f) for f in h.filters] == [ScrubFilter]
    finally:
        root.removeHandler(h)


def test_warn_if_loose_perms(tmp_path, caplog):
    loose, ok = tmp_path / "loose", tmp_path / "ok"
    loose.write_text("x")
    ok.write_text("x")
    loose.chmod(0o666)
    ok.chmod(0o600)
    with caplog.at_level(logging.WARNING, logger="alerts.scrub"):
        warn_if_loose_perms(loose, ok, tmp_path / "missing")
    assert len(caplog.records) == 1
    assert "loose" in caplog.text
    assert "666" in caplog.text


def test_secret_with_html_chars_is_scrubbed_even_after_esc(monkeypatch):
    monkeypatch.setenv("KITE_API_SECRET", "ab&c<d>efgh")
    out = scrub(f"raw ab&c<d>efgh / escaped {esc('ab&c<d>efgh')}")
    assert "efgh" not in out
    assert out.count(REDACTED) == 2


def test_directory_mask_keeps_755_quiet(tmp_path, caplog):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o755)
    with caplog.at_level(logging.WARNING, logger="alerts.scrub"):
        warn_if_loose_perms(d, mask=0o022)
        assert not caplog.records
        d.chmod(0o777)
        warn_if_loose_perms(d, mask=0o022)
    assert len(caplog.records) == 1
