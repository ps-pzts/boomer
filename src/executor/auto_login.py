"""Fully automated broker token refresh using TOTP.

Both brokers require a daily access-token refresh. This module handles
the full OAuth/login flow programmatically so the orchestrator can run
unattended.

Kite flow:
    POST /api/login (password) → POST /api/twofa (TOTP) →
    follow Kite Connect redirect → extract request_token → generate_session

Fyers flow:
    POST send-login-otp → POST verify-otp (TOTP) →
    POST verify-pin → extract auth_code → generate_token

Required env vars per broker:
    Kite:   KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
    Fyers:  FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI,
            FYERS_USER_ID, FYERS_PIN, FYERS_TOTP_SECRET
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

_FYERS_SEND_OTP_URL = "https://api-t2.fyers.in/vagator/v2/send_login_otp"
_FYERS_VERIFY_OTP_URL = "https://api-t2.fyers.in/vagator/v2/verify_otp"
_FYERS_VERIFY_PIN_URL = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"


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


def fyers_auto_login(
    client_id: str,
    secret_key: str,
    redirect_uri: str,
    user_id: str,
    pin: str,
    totp_secret: str,
) -> str:
    """Return a fresh Fyers access_token via automated TOTP login.

    pin is the Fyers MPIN (the 4-digit quick-login PIN set in the Fyers mobile app).
    """
    # Fyers API v3 uses the app portion of client_id (before the last '-NNN')
    app_id = client_id.rsplit("-", 1)[0]

    # Step 1: initiate login — Fyers returns request_key
    r = requests.post(
        _FYERS_SEND_OTP_URL,
        json={"fy_id": user_id, "app_id": app_id},
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"Fyers send-login-otp HTTP {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("s") != "ok" and body.get("code") not in (200, 1043):
        raise RuntimeError(f"Fyers send-login-otp failed: {body}")
    request_key = body["request_key"]
    logger.debug("fyers_auto_login: send-login-otp ok request_key=%s…", request_key[:8])

    # Step 2: verify TOTP
    totp = pyotp.TOTP(totp_secret).now()
    r = requests.post(
        _FYERS_VERIFY_OTP_URL,
        json={"request_key": request_key, "otp": totp},
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"Fyers verify-otp HTTP {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("s") != "ok" and body.get("code") not in (200, 1043):
        raise RuntimeError(f"Fyers verify-otp failed: {body}")
    request_key = body["request_key"]
    logger.debug("fyers_auto_login: verify-otp ok")

    # Step 3: verify PIN (Fyers MPIN — the 4-digit quick-login PIN from the mobile app)
    r = requests.post(
        _FYERS_VERIFY_PIN_URL,
        json={
            "request_key": request_key,
            "identity_type": "pin",
            "identifier": user_id,
            "pin": pin,
        },
        timeout=15,
    )
    if not r.ok:
        body_text = r.text[:500]
        if "-1006" in body_text or "Invalid PIN" in body_text:
            raise RuntimeError(
                "Fyers verify-pin: wrong MPIN — check FYERS_PIN in .env. "
                "This is the 4-digit PIN from the Fyers mobile app. "
                "If you just changed it, wait 2-3 minutes before retrying (propagation delay)."
            )
        raise RuntimeError(f"Fyers verify-pin HTTP {r.status_code}: {body_text}")
    body = r.json()
    if body.get("s") != "ok" and body.get("code") not in (200, 1043):
        raise RuntimeError(f"Fyers verify-pin failed: {body}")

    session_token = body.get("data", {}).get("token")
    if not session_token:
        raise RuntimeError(f"Fyers verify-pin did not return session token: {body}")
    logger.debug("fyers_auto_login: verify-pin ok, have session token")

    # Step 4: use session token to complete OAuth and get auth_code
    from fyers_apiv3.fyersModel import SessionModel  # type: ignore[import-untyped]

    sess = SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    auth_url = sess.generate_authcode()
    r = requests.get(
        auth_url,
        headers={"Authorization": f"Bearer {session_token}"},
        allow_redirects=False,
        timeout=15,
    )
    location = r.headers.get("Location", "")
    params = parse_qs(urlparse(location).query)
    auth_code = (params.get("auth_code") or params.get("code") or [None])[0]
    if not auth_code:
        raise RuntimeError(
            f"Fyers OAuth did not return auth_code in redirect Location: {location!r}"
        )
    logger.debug("fyers_auto_login: auth_code obtained auth_code=%s…", auth_code[:8])

    # Step 5: exchange auth_code for access_token
    sess.set_token(auth_code)
    response = sess.generate_token()
    if "access_token" not in response:
        raise RuntimeError(f"Fyers token exchange failed: {response}")
    access_token = response["access_token"]
    logger.info("fyers_auto_login: success user_id=%s", user_id)
    return access_token


def refresh_all_broker_tokens() -> dict[str, str]:
    """Refresh tokens for all configured brokers.

    Returns a dict mapping broker name → new access token for each
    broker that had TOTP credentials configured. Skips brokers that
    lack TOTP credentials without raising.

    Side effects:
        - Updates os.environ with new tokens
        - Writes new tokens back to .env so they survive restarts
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
            _notify(f"Kite login successful — token refreshed for {kite_user}")
        except Exception as exc:
            logger.error("refresh_all_broker_tokens: kite failed — %s", exc)
            _notify(f"Kite auto-login FAILED — {exc}", error=True)
    else:
        logger.debug("refresh_all_broker_tokens: kite TOTP credentials not set, skipping")

    fyers_client = os.environ.get("FYERS_CLIENT_ID", "")
    fyers_secret = os.environ.get("FYERS_SECRET_KEY", "")
    fyers_redirect = os.environ.get("FYERS_REDIRECT_URI", "https://127.0.0.1")
    fyers_user = os.environ.get("FYERS_USER_ID", "")
    fyers_pin = os.environ.get("FYERS_PIN", "")
    fyers_totp = os.environ.get("FYERS_TOTP_SECRET", "")

    if all([fyers_client, fyers_secret, fyers_user, fyers_pin, fyers_totp]):
        try:
            token = fyers_auto_login(
                fyers_client, fyers_secret, fyers_redirect, fyers_user, fyers_pin, fyers_totp
            )
            os.environ["FYERS_ACCESS_TOKEN"] = token
            _write_env_token(_ENV_PATH, "FYERS_ACCESS_TOKEN", token)
            updated["fyers"] = token
            logger.info("refresh_all_broker_tokens: fyers token refreshed")
            _notify(f"Fyers login successful — token refreshed for {fyers_user}")
        except Exception as exc:
            logger.error("refresh_all_broker_tokens: fyers failed — %s", exc)
            _notify(f"Fyers auto-login FAILED — {exc}", error=True)
    else:
        logger.debug("refresh_all_broker_tokens: fyers TOTP credentials not set, skipping")

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
