"""kite_session: the SDK-touching login steps (#126). Offline: login_url builds a string only."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from executor.brokers.kite_session import exchange_request_token, login_url


def test_login_url_carries_the_api_key():
    url = login_url("my_key")
    assert url.startswith("https://kite.zerodha.com/connect/login")
    assert "api_key=my_key" in url


def test_exchange_returns_access_token():
    sdk = MagicMock()
    sdk.generate_session.return_value = {"access_token": "tok"}
    with patch("kiteconnect.KiteConnect", return_value=sdk) as cls:
        assert exchange_request_token("k", "s", "rt") == "tok"
    cls.assert_called_once_with(api_key="k")
    sdk.generate_session.assert_called_once_with("rt", api_secret="s")


def test_exchange_propagates_sdk_failure():
    sdk = MagicMock()
    sdk.generate_session.side_effect = RuntimeError("bad token")
    with patch("kiteconnect.KiteConnect", return_value=sdk), pytest.raises(RuntimeError):
        exchange_request_token("k", "s", "rt")
