"""Kite (Zerodha) daily token refresh.

Run once each morning before 9 AM IST to get a fresh KITE_ACCESS_TOKEN.

Required env vars (set in .env):
    KITE_API_KEY     — from your Kite developer app
    KITE_API_SECRET  — from your Kite developer app

Usage:
    PYTHONPATH=src .venv/bin/python scripts/kite_login.py
"""

import os
import sys
from pathlib import Path

# Load .env from repo root so the script works without manually sourcing it
_env_path = Path(__file__).parents[1] / ".env"
if _env_path.exists():
    from dotenv import load_dotenv

    load_dotenv(_env_path, override=False)


def main() -> None:
    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_API_SECRET", "")

    if not api_key or not api_secret:
        print("ERROR: Set KITE_API_KEY and KITE_API_SECRET in your .env file.")
        sys.exit(1)

    from kiteconnect import KiteConnect  # type: ignore[import-untyped]

    kite = KiteConnect(api_key=api_key)

    print("\n── Kite Login ────────────────────────────────────────")
    print("1. Open this URL in your browser:")
    print(f"\n   {kite.login_url()}\n")
    print("2. Log in with your Zerodha credentials.")
    print("3. After login you will be redirected to a URL like:")
    print("   https://127.0.0.1/?request_token=XXXXXXXX&action=login&status=success")
    print("4. Copy the request_token value from that URL.")
    print("─────────────────────────────────────────────────────\n")

    request_token = input("Paste request_token here: ").strip()
    if not request_token:
        print("ERROR: No token entered.")
        sys.exit(1)

    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        print(f"ERROR: Token exchange failed — {exc}")
        sys.exit(1)

    access_token = data["access_token"]

    print("\n── Done ──────────────────────────────────────────────")
    print(f"KITE_ACCESS_TOKEN={access_token}")
    print("\nAdd or update this line in your .env file, then restart:")
    print("    ./dev.sh stop && ./dev.sh")
    print("─────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
