"""Kite login steps that touch the SDK (CLAUDE.md §9: SDK only inside executor/brokers/)."""

from __future__ import annotations


def login_url(api_key: str) -> str:
    from kiteconnect import KiteConnect  # type: ignore[import-untyped]

    return KiteConnect(api_key=api_key).login_url()


def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> str:
    """Swap the one-shot request_token for the day's access_token."""
    from kiteconnect import KiteConnect  # type: ignore[import-untyped]

    data = KiteConnect(api_key=api_key).generate_session(request_token, api_secret=api_secret)
    return data["access_token"]
