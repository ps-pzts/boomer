"""Standalone broker token refresh using TOTP auto-login.

Run this to test automated login or to manually trigger a token refresh
outside the orchestrator's scheduled window.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/auto_login.py
    PYTHONPATH=src .venv/bin/python scripts/auto_login.py --broker kite
    PYTHONPATH=src .venv/bin/python scripts/auto_login.py --broker fyers

Required in .env:
    Kite:  KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
    Fyers: FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI,
           FYERS_USER_ID, FYERS_PIN, FYERS_TOTP_SECRET
"""

import argparse
import logging
import os
import sys
from pathlib import Path

_env_path = Path(__file__).parents[1] / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--broker",
        choices=["kite", "fyers", "all"],
        default="all",
        help="Which broker to refresh (default: all)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show verbose HTTP step-by-step output",
    )
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    from executor.auto_login import (
        _write_env_token,
        fyers_auto_login,
        kite_auto_login,
        refresh_all_broker_tokens,
    )

    if args.broker == "all":
        updated = refresh_all_broker_tokens()
        if not updated:
            print(
                "No brokers refreshed. Make sure KITE_TOTP_SECRET / FYERS_TOTP_SECRET "
                "are set in .env."
            )
            sys.exit(1)
        for broker, token in updated.items():
            print(f"\n── {broker.upper()} ──────────────────────────────────────")
            print(f"New token (first 12 chars): {token[:12]}…")
        print("\nTokens written to .env — restart the orchestrator to pick them up.")

    elif args.broker == "kite":
        required = [
            "KITE_API_KEY", "KITE_API_SECRET", "KITE_USER_ID", "KITE_PASSWORD",
            "KITE_TOTP_SECRET",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            print(f"ERROR: Missing env vars: {', '.join(missing)}")
            sys.exit(1)
        token = kite_auto_login(
            os.environ["KITE_API_KEY"],
            os.environ["KITE_API_SECRET"],
            os.environ["KITE_USER_ID"],
            os.environ["KITE_PASSWORD"],
            os.environ["KITE_TOTP_SECRET"],
        )
        os.environ["KITE_ACCESS_TOKEN"] = token
        _write_env_token(_env_path, "KITE_ACCESS_TOKEN", token)
        print("\n── KITE ─────────────────────────────────────────────")
        print(f"KITE_ACCESS_TOKEN={token}")
        print("\nToken written to .env — restart the orchestrator to pick it up.")

    elif args.broker == "fyers":
        required = [
            "FYERS_CLIENT_ID", "FYERS_SECRET_KEY", "FYERS_USER_ID", "FYERS_PIN",
            "FYERS_TOTP_SECRET",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            print(f"ERROR: Missing env vars: {', '.join(missing)}")
            sys.exit(1)
        token = fyers_auto_login(
            os.environ["FYERS_CLIENT_ID"],
            os.environ["FYERS_SECRET_KEY"],
            os.environ.get("FYERS_REDIRECT_URI", "https://127.0.0.1"),
            os.environ["FYERS_USER_ID"],
            os.environ["FYERS_PIN"],
            os.environ["FYERS_TOTP_SECRET"],
        )
        os.environ["FYERS_ACCESS_TOKEN"] = token
        _write_env_token(_env_path, "FYERS_ACCESS_TOKEN", token)
        print("\n── FYERS ────────────────────────────────────────────")
        print(f"FYERS_ACCESS_TOKEN={token}")
        print("\nToken written to .env — restart the orchestrator to pick it up.")


if __name__ == "__main__":
    main()
