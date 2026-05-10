"""One-shot Fyers token generator.

Run once each morning before market open (token expires after 24 hours).

Usage:
    python scripts/fyers_login.py

Steps:
    1. Opens the Fyers auth URL in your browser
    2. You log in and are redirected with ?auth_code=...
    3. Paste the auth_code here
    4. Script prints FYERS_ACCESS_TOKEN — add it to your .env or secrets.env

Environment variables required:
    FYERS_CLIENT_ID    — your app's client_id (format: APPID-100)
    FYERS_SECRET_KEY   — your app's secret key
    FYERS_REDIRECT_URI — the redirect URI you registered (e.g. https://127.0.0.1/)
"""
from __future__ import annotations

import os
import sys
import webbrowser

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    sys.exit("fyers-apiv3 not installed — run: pip install fyers-apiv3")


def main() -> None:
    client_id = os.environ.get("FYERS_CLIENT_ID") or input("FYERS_CLIENT_ID: ").strip()
    secret_key = os.environ.get("FYERS_SECRET_KEY") or input("FYERS_SECRET_KEY: ").strip()
    redirect_uri = (
        os.environ.get("FYERS_REDIRECT_URI") or
        input("FYERS_REDIRECT_URI (e.g. https://127.0.0.1/): ").strip()
    )

    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )

    auth_url = session.generate_authcode()
    print(f"\nOpening Fyers auth URL:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    auth_code = input(
        "Paste the auth_code from the redirect URL\n"
        "(it appears as ?auth_code=xxx in the browser address bar): "
    ).strip()

    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("code") != 200:
        sys.exit(f"Token generation failed: {response}")

    access_token = response["access_token"]
    print("\nSuccess! Add this to your .env:\n")
    print(f"  FYERS_CLIENT_ID={client_id}")
    print(f"  FYERS_ACCESS_TOKEN={access_token}")
    print()


if __name__ == "__main__":
    main()
