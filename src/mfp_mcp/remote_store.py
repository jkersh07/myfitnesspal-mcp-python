"""Session storage for headless deployments.

The stock server finds its MyFitnessPal session in one of four places, in
order: a form login, a file at ~/.mfp_mcp/cookies.json, a Chromium browser's
cookie database on macOS, and browser_cookie3. None of those exist on a
serverless host. There is no browser, no macOS keychain, and no disk that
survives from one request to the next, and the form login stopped working when
MyFitnessPal moved to NextAuth (see authenticate_with_credentials in
server.py, which says so and raises).

So the file is replaced with a small key-value store reached over HTTPS. A
session is pushed once from a Mac with scripts/push_cookie.py, and every
successful request writes the current session back, which is what keeps it
alive if MyFitnessPal rolls its expiry forward on use.

Configuration comes from the environment:
    UPSTASH_REDIS_REST_URL
    UPSTASH_REDIS_REST_TOKEN
    MFP_COOKIE_KEY          optional, defaults to "mfp:cookies"
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import httpx

logger = logging.getLogger("mfp_mcp.remote_store")

DEFAULT_KEY = "mfp:cookies"

# Matches the stock load_cookies() guard. A session older than this is treated
# as dead without spending a request on it. Every successful call refreshes the
# stamp, so this only trips after a real gap in use.
MAX_AGE = timedelta(days=30)


class StoreNotConfigured(RuntimeError):
    pass


def _rest_credentials() -> tuple[str, str]:
    """The Upstash REST URL and token, under either naming scheme.

    Upstash's own docs use UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN;
    Vercel's marketplace integration injects the same values as
    KV_REST_API_URL / KV_REST_API_TOKEN. Accept both, preferring the former.
    """
    url = (
        os.environ.get("UPSTASH_REDIS_REST_URL")
        or os.environ.get("KV_REST_API_URL")
        or ""
    ).rstrip("/")
    token = (
        os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        or os.environ.get("KV_REST_API_TOKEN")
        or ""
    )
    return url, token


def _config() -> tuple[str, str, str]:
    url, token = _rest_credentials()
    if not url or not token:
        raise StoreNotConfigured(
            "Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN "
            "(or Vercel's KV_REST_API_URL and KV_REST_API_TOKEN)."
        )
    return url, token, os.environ.get("MFP_COOKIE_KEY", DEFAULT_KEY)


def is_configured() -> bool:
    try:
        _config()
        return True
    except StoreNotConfigured:
        return False


def load_cookies() -> Optional[Dict[str, str]]:
    """Return the stored session, or None if absent, unreadable, or stale.

    Signature matches the stock server.load_cookies so it can replace it.
    """
    url, token, key = _config()
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{url}/get/{key}",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            raw = resp.json().get("result")
    except Exception as exc:
        logger.warning("Could not read the stored session: %s", exc)
        return None

    if not raw:
        logger.info("No session in the store yet.")
        return None

    try:
        payload = json.loads(raw)
        saved_at = datetime.fromisoformat(payload["saved_at"])
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - saved_at > MAX_AGE:
            logger.info("Stored session is older than %s.", MAX_AGE)
            return None
        return payload["cookies"]
    except Exception as exc:
        logger.warning("Stored session is malformed: %s", exc)
        return None


def save_cookies(cookies: Dict[str, str]) -> None:
    """Persist the session and stamp it with the current time.

    Signature matches the stock server.save_cookies so it can replace it.
    """
    url, token, key = _config()
    payload = json.dumps(
        {
            "cookies": cookies,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{url}/set/{key}",
                headers={"Authorization": f"Bearer {token}"},
                content=payload,
            )
            resp.raise_for_status()
        logger.info("Stored session updated (%d cookies).", len(cookies))
    except Exception as exc:
        # A failed write must not fail the user's request. The session in hand
        # still works; only the refresh is lost.
        logger.warning("Could not write the session back: %s", exc)
