"""One-shot Kite Connect token generator.

Run once each morning before market open (token expires at midnight IST).

Usage:
    python scripts/kite_login.py

Steps:
    1. Opens the Kite login URL in your browser
    2. You log in and are redirected to your redirect URL with ?request_token=...
    3. Paste the full redirect URL here
    4. Script prints KITE_ACCESS_TOKEN — add it to your .env or secrets.env

Environment variables required:
    KITE_API_KEY       — from https://developers.kite.trade/
    KITE_API_SECRET    — from the same app page
"""
from __future__ import annotations

import os
import sys
import webbrowser

try:
    from kiteconnect import KiteConnect
except ImportError:
    sys.exit("kiteconnect not installed — run: pip install kiteconnect")


def main() -> None:
    api_key = os.environ.get("KITE_API_KEY") or input("KITE_API_KEY: ").strip()
    api_secret = os.environ.get("KITE_API_SECRET") or input("KITE_API_SECRET: ").strip()

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    print(f"\nOpening login URL:\n  {login_url}\n")
    webbrowser.open(login_url)

    redirect = input(
        "Paste the full redirect URL after login\n"
        "(looks like https://your-redirect/?request_token=xxx&...): "
    ).strip()

    # Parse request_token from URL
    from urllib.parse import parse_qs, urlparse
    params = parse_qs(urlparse(redirect).query)
    request_token_list = params.get("request_token")
    if not request_token_list:
        sys.exit("No request_token found in the URL. Did you paste the redirect URL?")
    request_token = request_token_list[0]

    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]

    print("\nSuccess! Add this to your .env:\n")
    print(f"  KITE_API_KEY={api_key}")
    print(f"  KITE_ACCESS_TOKEN={access_token}")
    print()


if __name__ == "__main__":
    main()
