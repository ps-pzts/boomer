"""Standalone Kite token refresh using TOTP auto-login.

Run this to test automated login or to manually trigger a token refresh
outside the orchestrator's scheduled window.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/auto_login.py

Required in .env:
    KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
"""

import argparse
import logging
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
        "--debug",
        action="store_true",
        help="Show verbose HTTP step-by-step output",
    )
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    from executor.auto_login import refresh_all_broker_tokens

    updated = refresh_all_broker_tokens()
    if not updated:
        print("No broker refreshed. Make sure KITE_TOTP_SECRET is set in .env.")
        sys.exit(1)
    for broker, token in updated.items():
        print(f"\n── {broker.upper()} ──────────────────────────────────────")
        print(f"New token (first 12 chars): {token[:12]}…")
    print("\nToken written to .env — restart the orchestrator to pick it up.")


if __name__ == "__main__":
    main()
