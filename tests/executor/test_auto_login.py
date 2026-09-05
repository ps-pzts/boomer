"""Tests for src/executor/auto_login.py — automated Kite TOTP login."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from executor.auto_login import (
    _kite_extract_request_token,
    _write_env_token,
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
