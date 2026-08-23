"""Vercel entry point for the MyFitnessPal MCP server.

Nothing in server.py is edited, so pulling upstream fixes later stays a clean
merge. Three things happen here.

1. The server is switched from stdio (one process per Claude Code session) to
   streamable HTTP in stateless mode.

   The obvious way to do that, FastMCP's own `streamable_http_app()`, does not
   work on Vercel. That app starts its session manager's task group in the ASGI
   *lifespan*, and serverless platforms never run lifespan events; every request
   would fail with "Task group is not initialized." Running the manager by hand
   per request does not work either, because `run()` refuses to be called twice
   on one instance and a warm container serves many requests from one process.

   So a fresh session manager is built per request. In stateless mode that is
   what the transport does internally anyway, and the expensive part (the
   FastMCP server and its twenty registered tools) is built once at import and
   shared.

2. get_mfp_client is replaced. The stock version tries a form login, a local
   file, a macOS browser, and browser_cookie3, in that order. The form login is
   broken upstream against MyFitnessPal's NextAuth backend (the stock code says
   so and raises), and the other three cannot work without a Mac. The
   replacement reads one session from the store and fails with a message that
   says what to do.

3. Every successful call writes the session back. The stock server saves only
   after a fresh browser or credential login, never after using a stored one,
   so a session that MyFitnessPal refreshed was being discarded. That omission
   is the difference between a session that sustains itself and one that dies
   on a fixed date.

Environment:
    MCP_AUTH_TOKEN            required. Callers send it as a bearer token.
    UPSTASH_REDIS_REST_URL    required. See remote_store.
    UPSTASH_REDIS_REST_TOKEN  required.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from datetime import date

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("mfp_mcp.vercel")

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402

from mfp_mcp import remote_store  # noqa: E402
from mfp_mcp import server as mfp  # noqa: E402

# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------

mfp.load_cookies = remote_store.load_cookies
mfp.save_cookies = remote_store.save_cookies


def _current_cookies(client) -> dict:
    """Read the live session back off the client.

    myfitnesspal.Client wraps a requests.Session in cloudscraper, so
    client.session.cookies is a RequestsCookieJar.
    """
    return {c.name: c.value for c in client.session.cookies}


def get_mfp_client_remote():
    """Build a client from the stored session, verify it, and refresh it."""
    import myfitnesspal

    cookies = remote_store.load_cookies()
    if not cookies:
        raise RuntimeError(
            "No MyFitnessPal session is stored. Run scripts/push_cookie.py on "
            "a Mac that is logged into myfitnesspal.com to publish one."
        )

    client = myfitnesspal.Client(cookiejar=mfp.dict_to_cookiejar(cookies))

    try:
        # The stock ladder uses this same call as its liveness check.
        client.get_date(date.today())
    except Exception as exc:
        raise RuntimeError(
            f"The stored MyFitnessPal session was rejected ({exc}). Run "
            "scripts/push_cookie.py on a Mac that is logged into "
            "myfitnesspal.com to publish a fresh one."
        ) from exc

    # Persist whatever the session looks like now. If MyFitnessPal rolled the
    # expiry forward, this is what keeps it.
    remote_store.save_cookies(_current_cookies(client))
    return client


mfp.get_mfp_client = get_mfp_client_remote

# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

# Built once per process and shared. This holds the tool registry.
_MCP_SERVER = mfp.mcp._mcp_server

_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


def _new_session_manager() -> StreamableHTTPSessionManager:
    return StreamableHTTPSessionManager(
        app=_MCP_SERVER,
        json_response=True,
        stateless=True,
    )


async def _reject(send, status: int, message: str) -> None:
    body = message.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _authorized(scope) -> bool:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            presented = value.decode(errors="ignore")
            prefix = "Bearer "
            if not presented.startswith(prefix):
                return False
            return hmac.compare_digest(presented[len(prefix):], _TOKEN)
    return False


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        # Nothing to start or stop: each request builds its own manager.
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope["type"] != "http":
        await _reject(send, 400, "Unsupported protocol.")
        return

    if not _TOKEN:
        # Refuse rather than serve an open endpoint that can write to a diary.
        await _reject(send, 500, "MCP_AUTH_TOKEN is not configured.")
        return

    if not _authorized(scope):
        await _reject(send, 401, "Unauthorized.")
        return

    # Vercel routes every path to this function; the transport expects its own.
    scope = dict(scope)
    scope["path"] = "/"
    scope["raw_path"] = b"/"

    manager = _new_session_manager()
    async with manager.run():
        await manager.handle_request(scope, receive, send)
