"""Fyers daily token refresh.

Run once each morning before 9 AM IST to get a fresh FYERS_ACCESS_TOKEN.

Required env vars (set in .env):
    FYERS_CLIENT_ID    — your Fyers app client ID (e.g. XY12345-100)
    FYERS_SECRET_KEY   — your Fyers app secret key
    FYERS_REDIRECT_URI — redirect URI set in your Fyers app (default: https://127.0.0.1)

Usage:
    PYTHONPATH=src .venv/bin/python scripts/fyers_login.py
"""

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Load .env from repo root so the script works without manually sourcing it
_env_path = Path(__file__).parents[1] / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=False)


def _extract_auth_code(raw: str) -> str:
    """Accept either the full redirect URL or just the bare code value."""
    raw = raw.strip()
    if raw.startswith("http"):
        params = parse_qs(urlparse(raw).query)
        # Fyers uses 'auth_code' in some SDK versions and 'code' in others
        for key in ("auth_code", "code"):
            if key in params:
                return params[key][0]
        raise ValueError(f"Could not find auth_code or code in URL: {raw}")
    return raw


def main() -> None:
    client_id = os.environ.get("FYERS_CLIENT_ID", "")
    secret_key = os.environ.get("FYERS_SECRET_KEY", "")
    redirect_uri = os.environ.get("FYERS_REDIRECT_URI", "https://127.0.0.1")

    if not client_id or not secret_key:
        print("ERROR: Set FYERS_CLIENT_ID and FYERS_SECRET_KEY in your .env file.")
        sys.exit(1)

    from fyers_apiv3.fyersModel import SessionModel  # type: ignore[import-untyped]

    session = SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )

    print("\n── Fyers Login ───────────────────────────────────────")
    print("1. Open this URL in your browser:")
    print(f"\n   {session.generate_authcode()}\n")
    print("2. Log in with your Fyers credentials and complete 2FA.")
    print("3. You will be redirected to a URL like:")
    print("   https://127.0.0.1/?s=ok&code=XXXXXXXX&state=None")
    print("4. Copy the ENTIRE redirect URL from your browser address bar.")
    print("─────────────────────────────────────────────────────\n")

    raw = input("Paste the full redirect URL (or just the code): ").strip()
    if not raw:
        print("ERROR: Nothing entered.")
        sys.exit(1)

    try:
        auth_code = _extract_auth_code(raw)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"Using auth_code: {auth_code[:8]}…")

    try:
        session.set_token(auth_code)
        response = session.generate_token()
    except Exception as exc:
        print(f"ERROR: Token exchange failed — {exc}")
        sys.exit(1)

    if "access_token" not in response:
        print(f"ERROR: Fyers returned an error — {response}")
        sys.exit(1)

    access_token = response["access_token"]

    print("\n── Done ──────────────────────────────────────────────")
    print(f"FYERS_ACCESS_TOKEN={access_token}")
    print("\nAdd or update this line in your .env file, then restart:")
    print("    ./dev.sh stop && ./dev.sh")
    print("─────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
