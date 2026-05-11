"""Tests for src/executor/auto_login.py — automated broker TOTP login."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from executor.auto_login import (
    _kite_extract_request_token,
    _write_env_token,
    fyers_auto_login,
    kite_auto_login,
    refresh_all_broker_tokens,
)

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
    env.write_text("FYERS_ACCESS_TOKEN=oldtoken\n")
    _write_env_token(env, "FYERS_ACCESS_TOKEN", "")
    assert "FYERS_ACCESS_TOKEN=" in env.read_text()


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
def test_kite_auto_login_success(mock_totp, mock_extract, mock_session_cls):
    mock_totp.return_value.now.return_value = "123456"

    sess = MagicMock()
    mock_session_cls.return_value = sess
    sess.post.side_effect = [
        _make_response({"status": "success", "data": {"request_id": "req_abc"}}),
        _make_response({"status": "success", "data": {}}),
    ]
    mock_extract.return_value = "request_token_xyz"

    mock_kite = MagicMock()
    mock_kite.generate_session.return_value = {"access_token": "access_abc"}

    with patch("kiteconnect.KiteConnect", return_value=mock_kite):
        token = kite_auto_login("api_key", "api_secret", "user1", "pass1", "TOTP_SECRET")

    assert token == "access_abc"
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


# ── fyers_auto_login ──────────────────────────────────────────────────────────


@patch("executor.auto_login.requests.get")
@patch("executor.auto_login.requests.post")
@patch("executor.auto_login.pyotp.TOTP")
def test_fyers_auto_login_success(mock_totp, mock_post, mock_get):
    mock_totp.return_value.now.return_value = "654321"
    mock_post.side_effect = [
        _make_response({"s": "ok", "request_key": "rk1"}),
        _make_response({"s": "ok", "request_key": "rk2"}),
        _make_response({"s": "ok", "data": {"token": "session_tok"}}),
    ]
    mock_get.return_value = _make_response(
        {}, headers={"Location": "https://127.0.0.1/?auth_code=auth_xyz&state=None"}
    )

    mock_sess = MagicMock()
    mock_sess.generate_authcode.return_value = "https://auth.fyers.in/..."
    mock_sess.generate_token.return_value = {"access_token": "fyers_token"}

    with patch("fyers_apiv3.fyersModel.SessionModel", return_value=mock_sess):
        token = fyers_auto_login(
            "APPID-100", "secret", "https://127.0.0.1", "user1", "mypassword", "TOTP"
        )

    assert token == "fyers_token"
    mock_sess.set_token.assert_called_once_with("auth_xyz")
    # app_id stripped of suffix
    first_call_kwargs = mock_post.call_args_list[0]
    assert first_call_kwargs[1]["json"]["app_id"] == "APPID"
    # verify MPIN is sent with identity_type=pin
    third_call = mock_post.call_args_list[2]
    assert third_call[1]["json"]["identity_type"] == "pin"
    assert third_call[1]["json"]["pin"] == "mypassword"


@patch("executor.auto_login.requests.post")
@patch("executor.auto_login.pyotp.TOTP")
def test_fyers_auto_login_bad_otp(mock_totp, mock_post):
    mock_totp.return_value.now.return_value = "000000"
    mock_post.side_effect = [
        _make_response({"s": "ok", "request_key": "rk1"}),
        _make_response({"s": "error", "message": "Invalid OTP"}),
    ]

    with pytest.raises(RuntimeError, match="verify-otp failed"):
        fyers_auto_login("APP-100", "s", "https://127.0.0.1", "u", "password", "totp")


@patch("executor.auto_login.requests.get")
@patch("executor.auto_login.requests.post")
@patch("executor.auto_login.pyotp.TOTP")
def test_fyers_auto_login_code_param_fallback(mock_totp, mock_post, mock_get):
    """Some Fyers redirect URLs use 'code' instead of 'auth_code'."""
    mock_totp.return_value.now.return_value = "123456"
    mock_post.side_effect = [
        _make_response({"s": "ok", "request_key": "rk1"}),
        _make_response({"s": "ok", "request_key": "rk2"}),
        _make_response({"s": "ok", "data": {"token": "sess_tok"}}),
    ]
    mock_get.return_value = _make_response(
        {}, headers={"Location": "https://127.0.0.1/?code=code_fallback&state=None"}
    )
    mock_sess = MagicMock()
    mock_sess.generate_authcode.return_value = "https://auth.fyers.in/..."
    mock_sess.generate_token.return_value = {"access_token": "token_ok"}

    with patch("fyers_apiv3.fyersModel.SessionModel", return_value=mock_sess):
        token = fyers_auto_login("APP-100", "s", "https://127.0.0.1", "u", "mypassword", "totp")

    assert token == "token_ok"
    mock_sess.set_token.assert_called_once_with("code_fallback")


# ── refresh_all_broker_tokens ─────────────────────────────────────────────────


def test_refresh_skips_when_totp_secrets_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("KITE_TOTP_SECRET", raising=False)
    monkeypatch.delenv("FYERS_TOTP_SECRET", raising=False)
    with patch("executor.auto_login._ENV_PATH", tmp_path / ".env"):
        updated = refresh_all_broker_tokens()
    assert updated == {}


@patch("executor.auto_login.kite_auto_login", return_value="new_kite_token")
@patch("executor.auto_login.fyers_auto_login")
def test_refresh_updates_env_and_env_file(mock_fyers, mock_kite, tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    monkeypatch.setenv("KITE_USER_ID", "user")
    monkeypatch.setenv("KITE_PASSWORD", "pass")
    monkeypatch.setenv("KITE_TOTP_SECRET", "totp")
    monkeypatch.delenv("FYERS_TOTP_SECRET", raising=False)
    monkeypatch.delenv("FYERS_PIN", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text("KITE_ACCESS_TOKEN=old_token\n")

    with patch("executor.auto_login._ENV_PATH", env_file):
        updated = refresh_all_broker_tokens()

    assert updated == {"kite": "new_kite_token"}
    assert os.environ["KITE_ACCESS_TOKEN"] == "new_kite_token"
    assert "KITE_ACCESS_TOKEN=new_kite_token" in env_file.read_text()
    mock_fyers.assert_not_called()


@patch("executor.auto_login.kite_auto_login", side_effect=RuntimeError("login failed"))
def test_refresh_logs_error_does_not_raise(mock_kite, tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "key")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    monkeypatch.setenv("KITE_USER_ID", "user")
    monkeypatch.setenv("KITE_PASSWORD", "pass")
    monkeypatch.setenv("KITE_TOTP_SECRET", "totp")
    monkeypatch.delenv("FYERS_TOTP_SECRET", raising=False)
    monkeypatch.delenv("FYERS_PIN", raising=False)

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
