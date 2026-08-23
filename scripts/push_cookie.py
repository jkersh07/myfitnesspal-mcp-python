#!/usr/bin/env python3
"""Publish this Mac's MyFitnessPal session to the hosted server.

Run this once after deploying, and again on the rare occasion the hosted
server says the session was rejected. It reads the session the local connector
already has (or pulls a fresh one out of a Chromium browser) and writes it to
the same store the hosted server reads.

    python scripts/push_cookie.py

Reads UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN from the
environment, or from a .env file beside this script's repository root.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402


def load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def collect_session() -> dict:
    """Get a working session, preferring the one already on this machine."""
    from mfp_mcp import server as mfp
    import myfitnesspal

    candidates = []

    stored = mfp.load_cookies()
    if stored:
        candidates.append(("the local connector's saved session", stored))

    try:
        found = mfp.try_chromium_browsers_for_session_cookies()
        if found:
            browser, cookies = found
            candidates.append((f"your {browser} session", cookies))
    except Exception as exc:
        print(f"  (browser lookup skipped: {exc})")

    if not candidates:
        sys.exit(
            "No MyFitnessPal session found on this Mac.\n"
            "Log into myfitnesspal.com in Chrome, Arc, Brave, or Edge, then run this again."
        )

    for label, cookies in candidates:
        print(f"Testing {label}...")
        try:
            client = myfitnesspal.Client(cookiejar=mfp.dict_to_cookiejar(cookies))
            client.get_date(date.today())
        except Exception as exc:
            print(f"  rejected: {exc}")
            continue
        print("  works.")
        # Return the post-request jar, which is what the server will store.
        return {c.name: c.value for c in client.session.cookies}

    sys.exit("Every session on this Mac was rejected by MyFitnessPal. Log in again and retry.")


def publish(cookies: dict) -> None:
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    key = os.environ.get("MFP_COOKIE_KEY", "mfp:cookies")

    if not url or not token:
        sys.exit(
            "Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN first.\n"
            "Both are on the storage database's page in the Vercel dashboard."
        )

    payload = json.dumps(
        {"cookies": cookies, "saved_at": datetime.now(timezone.utc).isoformat()}
    )
    resp = httpx.post(
        f"{url}/set/{key}",
        headers={"Authorization": f"Bearer {token}"},
        content=payload,
        timeout=15.0,
    )
    resp.raise_for_status()
    print(f"Published {len(cookies)} cookies. The hosted server can log meals now.")


if __name__ == "__main__":
    load_dotenv()
    publish(collect_session())
