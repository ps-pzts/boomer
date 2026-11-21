"""Tests for src/executor/auto_login.py — automated Kite TOTP login."""

from __future__ import annotations

import json
import logging
import os
import stat
from unittest.mock import MagicMock, patch

import pytest
import requests

from alerts.scrub import ScrubFilter
from executor.auto_login import (
    _kite_extract_request_token,
    _write_env_token,
    kite_auto_login,
    refresh_all_broker_tokens,
)
from tests import kite_fixtures as kf
from tests.kite_sdk import respond

# ── _write_env_token ──────────────────────────────────────────────────────────


def test_write_env_token_updates_existing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=old\nBAR=123\n")
    _write_env_token(env, "FOO", "new")
    lines = env.read_text().splitlines()
    assert "FOO=new" in lines
    assert "BAR=123" in lines


def test_write_env_token_appends_new_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=old\n")
    _write_env_token(env, "NEW_KEY", "value123")
    assert "NEW_KEY=value123" in env.read_text()


def test_write_env_token_noop_if_no_file(tmp_path):
    env = tmp_path / "nonexistent.env"
    # Should not raise
    _write_env_token(env, "KEY", "value")


def test_write_env_token_handles_empty_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KITE_ACCESS_TOKEN=oldtoken\n")
    _write_env_token(env, "KITE_ACCESS_TOKEN", "")
    assert "KITE_ACCESS_TOKEN=" in env.read_text()


# ── kite_auto_login ───────────────────────────────────────────────────────────


def _make_response(json_data, status_code=200, headers=None):
    m = MagicMock()
    m.json.return_value = json_data
    m.status_code = status_code
    m.headers = headers or {}
    m.raise_for_status = MagicMock()
    return m


@patch("executor.auto_login.requests.Session")
@patch("executor.auto_login._kite_extract_request_token")
@patch("executor.auto_login.pyotp.TOTP")
def test_kite_auto_login_success(mock_totp, mock_extract, mock_session_cls, kite_sdk):
    mock_totp.return_value.now.return_value = "123456"

    sess = MagicMock()
    mock_session_cls.return_value = sess
    sess.post.side_effect = [
        _make_response({"status": "success", "data": {"request_id": "req_abc"}}),
        _make_response({"status": "success", "data": {}}),
    ]
    mock_extract.return_value = "request_token_xyz"

    respond(kite_sdk, "generate_session", kf.sdk("generate_session"))

    with patch("kiteconnect.KiteConnect", return_value=kite_sdk) as kite_cls:
        token = kite_auto_login("api_key", "api_secret", "user1", "pass1", "TOTP_SECRET")

    assert token == "exampleaccesstoken"
    kite_cls.assert_called_once_with(api_key="api_key")
    kite_sdk.generate_session.assert_called_once_with("request_token_xyz", api_secret="api_secret")
    # TOTP was generated with correct secret
    mock_totp.assert_called_once_with("TOTP_SECRET")


@patch("executor.auto_login.requests.Session")
@patch("executor.auto_login.pyotp.TOTP")
def test_kite_auto_login_bad_password(mock_totp, mock_session_cls):
    mock_totp.return_value.now.return_value = "111111"
    sess = MagicMock()
    mock_session_cls.return_value = sess
    sess.post.return_value = _make_response({"status": "error", "message": "Invalid credentials"})

    with pytest.raises(RuntimeError, match="Kite login failed"):
        kite_auto_login("k", "s", "u", "wrong", "totp")


# ── refresh_all_broker_tokens ─────────────────────────────────────────────────


def test_refresh_skips_when_totp_secret_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("KITE_TOTP_SECRET", raising=False)
    with patch("executor.auto_login._ENV_PATH", tmp_path / ".env"):
        updated = refresh_all_broker_tokens()
    assert updated == {}


@patch("executor.auto_login.kite_auto_login", return_value="new_kite_token")
def test_refresh_updates_env_and_env_file(mock_kite, tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    monkeypatch.setenv("KITE_USER_ID", "user")
    monkeypatch.setenv("KITE_PASSWORD", "pass")
    monkeypatch.setenv("KITE_TOTP_SECRET", "totp")

    env_file = tmp_path / ".env"
    env_file.write_text("KITE_ACCESS_TOKEN=old_token\n")

    with patch("executor.auto_login._ENV_PATH", env_file):
        updated = refresh_all_broker_tokens()

    assert updated == {"kite": "new_kite_token"}
    assert os.environ["KITE_ACCESS_TOKEN"] == "new_kite_token"
    assert "KITE_ACCESS_TOKEN=new_kite_token" in env_file.read_text()


@patch("executor.auto_login.kite_auto_login", side_effect=RuntimeError("login failed"))
def test_refresh_logs_error_does_not_raise(mock_kite, tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    monkeypatch.setenv("KITE_USER_ID", "user")
    monkeypatch.setenv("KITE_PASSWORD", "pass")
    monkeypatch.setenv("KITE_TOTP_SECRET", "totp")

    with patch("executor.auto_login._ENV_PATH", tmp_path / ".env"):
        updated = refresh_all_broker_tokens()  # should not raise

    assert updated == {}


# ── _kite_extract_request_token ───────────────────────────────────────────────


def test_kite_extract_request_token_finds_token_in_location():
    sess = MagicMock()
    redirect = MagicMock()
    redirect.status_code = 302
    redirect.headers = {"Location": "https://127.0.0.1/?request_token=TOKEN123&action=login"}
    sess.get.return_value = redirect

    token = _kite_extract_request_token(sess, "my_api_key")
    assert token == "TOKEN123"


def test_kite_extract_request_token_raises_when_not_found():
    sess = MagicMock()
    r = MagicMock()
    r.status_code = 200
    r.headers = {}
    sess.get.return_value = r

    with pytest.raises(RuntimeError, match="request_token"):
        _kite_extract_request_token(sess, "key")


# ── secrets hygiene (#142) ────────────────────────────────────────────────────

_FAKE_TOKEN = "zq9fakeaccesstokenvalue123"


def _sent_texts(urlopen):
    return [json.loads(c[0][0].data)["text"] for c in urlopen.call_args_list]


def _login_env(monkeypatch):
    for k, v in dict(
        KITE_API_KEY="fakeapikey1",
        KITE_API_SECRET="fakeapisecret1",
        KITE_USER_ID="AB1234",
        KITE_PASSWORD="fakepassword1",
        KITE_TOTP_SECRET="FAKETOTPSECRET1",
        TELEGRAM_BOT_TOKEN="tok",
        TELEGRAM_CHAT_ID="1",
    ).items():
        monkeypatch.setenv(k, v)


@patch("executor.auto_login.kite_auto_login", return_value=_FAKE_TOKEN)
def test_refresh_notice_has_no_token_characters(_mock, tmp_path, monkeypatch):
    _login_env(monkeypatch)
    with (
        patch("executor.auto_login._ENV_PATH", tmp_path / ".env"),
        patch("alerts.telegram.urllib.request.urlopen") as up,
    ):
        refresh_all_broker_tokens()
    (text,) = _sent_texts(up)
    assert "AB1234" in text
    assert _FAKE_TOKEN[:4] not in text
    assert "Token" not in text


def test_refresh_failure_alert_and_log_are_scrubbed(tmp_path, monkeypatch, caplog):
    _login_env(monkeypatch)
    exc = requests.HTTPError("403 for url: https://x/login?request_token=reqtok999&api_key=k")
    caplog.handler.addFilter(ScrubFilter())
    with (
        patch("executor.auto_login.kite_auto_login", side_effect=exc),
        patch("executor.auto_login._ENV_PATH", tmp_path / ".env"),
        patch("alerts.telegram.urllib.request.urlopen") as up,
        caplog.at_level(logging.ERROR),
    ):
        refresh_all_broker_tokens()
    (text,) = _sent_texts(up)
    assert "FAILED" in text
    assert "reqtok999" not in text
    assert "reqtok999" not in caplog.handler.format(caplog.records[-1])


def test_write_env_token_is_0600_and_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# c\nA=1\nKITE_ACCESS_TOKEN=old\n\nB=two words\n")
    env.chmod(0o666)  # the mode it was found in
    _write_env_token(env, "KITE_ACCESS_TOKEN", "new")
    assert env.read_text() == "# c\nA=1\nKITE_ACCESS_TOKEN=new\n\nB=two words\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]  # temp file gone


def test_write_env_token_failure_leaves_original_and_no_temp(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    with (
        patch("executor.auto_login.os.replace", side_effect=OSError("disk")),
        pytest.raises(OSError),
    ):
        _write_env_token(env, "A", "2")
    assert env.read_text() == "A=1\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]
