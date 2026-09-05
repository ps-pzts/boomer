"""Fully automated Kite token refresh using TOTP.

Kite requires a daily access-token refresh. This module handles the full
OAuth/login flow programmatically so the orchestrator can run unattended.

Kite flow:
    POST /api/login (password) → POST /api/twofa (TOTP) →
    follow Kite Connect redirect → extract request_token → generate_session

Required env vars:
    KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import requests

logger = logging.getLogger(__name__)

# .env path relative to this file (two levels up to repo root)
_ENV_PATH = Path(__file__).parents[2] / ".env"

_KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
_KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
_KITE_CONNECT_URL = "https://kite.trade/connect/login"


def kite_auto_login(
    api_key: str,
    api_secret: str,
    user_id: str,
    password: str,
    totp_secret: str,
) -> str:
    """Return a fresh Kite access_token via automated TOTP login."""
    session = requests.Session()
    session.headers.update({"X-Kite-Version": "3"})

    # Step 1: password login
    r = session.post(
        _KITE_LOGIN_URL,
        data={"user_id": user_id, "password": password},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Kite login failed: {body}")
    request_id = body["data"]["request_id"]
    logger.debug("kite_auto_login: login ok request_id=%s", request_id)

    # Step 2: TOTP 2FA
    totp = pyotp.TOTP(totp_secret).now()
    r = session.post(
        _KITE_TWOFA_URL,
        data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": totp,
            "twofa_type": "totp",
        },
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Kite 2FA failed: {body}")
    logger.debug("kite_auto_login: 2FA ok")

    # Step 3: follow Kite Connect redirect to extract request_token
    request_token = _kite_extract_request_token(session, api_key)
    logger.debug("kite_auto_login: request_token=%s…", request_token[:8])

    # Step 4: exchange request_token for access_token
    from kiteconnect import KiteConnect  # type: ignore[import-untyped]

    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]
    logger.info("kite_auto_login: success user_id=%s", user_id)
    return access_token


def refresh_all_broker_tokens() -> dict[str, str]:
    """Refresh the Kite access token if TOTP credentials are configured.

    Returns a dict mapping broker name → new access token (empty if
    credentials are not configured). Does not raise on missing credentials.

    Side effects:
        - Updates os.environ with the new token
        - Writes the new token back to .env so it survives restarts
    """
    updated: dict[str, str] = {}

    kite_key = os.environ.get("KITE_API_KEY", "")
    kite_secret = os.environ.get("KITE_API_SECRET", "")
    kite_user = os.environ.get("KITE_USER_ID", "")
    kite_pass = os.environ.get("KITE_PASSWORD", "")
    kite_totp = os.environ.get("KITE_TOTP_SECRET", "")

    if all([kite_key, kite_secret, kite_user, kite_pass, kite_totp]):
        try:
            token = kite_auto_login(kite_key, kite_secret, kite_user, kite_pass, kite_totp)
            os.environ["KITE_ACCESS_TOKEN"] = token
            _write_env_token(_ENV_PATH, "KITE_ACCESS_TOKEN", token)
            updated["kite"] = token
            logger.info("refresh_all_broker_tokens: kite token refreshed")
            _notify(
                f"Kite token refreshed for <b>{kite_user}</b>\n"
                f"Token: <code>{token[:12]}…</code>\n"
                f"Expires: today 11:59 PM IST (Kite tokens reset at midnight)"
            )
        except Exception as exc:
            logger.error("refresh_all_broker_tokens: kite failed — %s", exc)
            _notify(
                f"Kite auto-login FAILED for <b>{kite_user}</b>\n"
                f"Error: {exc}\n"
                "Action: check KITE_PASSWORD / KITE_TOTP_SECRET in .env",
                error=True,
            )
    else:
        logger.debug("refresh_all_broker_tokens: kite TOTP credentials not set, skipping")

    return updated


# ── Private helpers ───────────────────────────────────────────────────────────


def _kite_extract_request_token(session: requests.Session, api_key: str) -> str:
    """Follow Kite Connect redirects and extract request_token without actually
    connecting to the (localhost) redirect URI."""
    url = f"{_KITE_CONNECT_URL}?api_key={api_key}&v=3"
    for _ in range(10):
        r = session.get(url, allow_redirects=False, timeout=15)
        location = r.headers.get("Location", "")
        if "request_token" in location:
            params = parse_qs(urlparse(location).query)
            tokens = params.get("request_token", [])
            if tokens:
                return tokens[0]
        if r.status_code in (301, 302, 303, 307, 308) and location:
            url = location
            continue
        break
    raise RuntimeError(
        "Kite Connect did not return request_token after following redirects. "
        "Check that the TOTP login succeeded and the api_key is correct."
    )


def _notify(message: str, error: bool = False) -> None:
    """Send a Telegram notification if credentials are configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    from alerts.telegram import send_telegram  # type: ignore[import-untyped]

    prefix = "🚨" if error else "✅"
    send_telegram(token, chat_id, f"{prefix} <b>Boomer</b>\n{message}")


def _write_env_token(env_path: Path, key: str, value: str) -> None:
    """Update KEY=value in .env file in-place, appending if the key is absent."""
    if not env_path.exists():
        return
    lines = env_path.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
