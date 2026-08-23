"""Exercise the Vercel wiring without touching MyFitnessPal.

Covers every branch api/index.py owns: missing token, wrong token, correct
token reaching the MCP app, a real tool call, the no-stored-session error path,
and a second request in the same process (the warm-container case that the
session manager's once-per-instance run() guard would otherwise break).

The live MyFitnessPal calls are not covered and cannot be until a real session
is published.
"""
import asyncio
import json
import os
import sys

os.environ["MCP_AUTH_TOKEN"] = "test-token-123"
os.environ["UPSTASH_REDIS_REST_URL"] = "https://example.invalid"
os.environ["UPSTASH_REDIS_REST_TOKEN"] = "fake"

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from api import index  # noqa: E402
from mfp_mcp import server as mfp  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


async def call(headers, body=b"", path="/mcp"):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "scheme": "https",
        "server": ("localhost", 443),
        "client": ("1.2.3.4", 1234),
    }
    sent = []
    received = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return received.pop(0) if received else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    await index.app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, payload


HDRS = [
    (b"content-type", b"application/json"),
    (b"accept", b"application/json, text/event-stream"),
]
AUTH = (b"authorization", b"Bearer test-token-123")


def rpc(method, params, rid):
    return json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    ).encode()


INIT = rpc(
    "initialize",
    {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
    1,
)


def main():
    # 1. Replacements are installed.
    check("get_mfp_client replaced", mfp.get_mfp_client is index.get_mfp_client_remote)
    check("load_cookies replaced", mfp.load_cookies.__module__ == "mfp_mcp.remote_store")
    check("save_cookies replaced", mfp.save_cookies.__module__ == "mfp_mcp.remote_store")

    # 2. Every tool survived the import.
    tools = asyncio.run(mfp.mcp.list_tools())
    names = sorted(t.name for t in tools)
    check("20 tools registered", len(tools) == 20, f"got {len(tools)}")
    for required in ("mfp_get_diary", "mfp_search_food", "mfp_add_food_to_diary", "mfp_remove_food_from_diary"):
        check(f"tool present: {required}", required in names)

    # 3. Auth branches.
    status, _ = asyncio.run(call(HDRS + [(b"authorization", b"Bearer wrong")], INIT))
    check("wrong token rejected", status == 401, f"status {status}")

    status, _ = asyncio.run(call(HDRS, INIT))
    check("missing header rejected", status == 401, f"status {status}")

    status, body = asyncio.run(call(HDRS + [AUTH], INIT))
    check("correct token reaches MCP app", status == 200, f"status {status}")
    check("initialize answered", b"serverInfo" in body, body[:150].decode(errors="replace"))

    # 4. Warm container: a second request in the same process must also work.
    #    This is the case that a single shared session manager would fail,
    #    because run() refuses a second call on one instance.
    status2, body2 = asyncio.run(call(HDRS + [AUTH], INIT))
    check("second request in same process", status2 == 200, f"status {status2}")

    # 5. A real tool listing over the transport.
    status3, body3 = asyncio.run(call(HDRS + [AUTH], rpc("tools/list", {}, 2)))
    check("tools/list over HTTP", status3 == 200 and b"mfp_get_diary" in body3, f"status {status3}")

    # 6. Unknown path still routes (Vercel sends every path to this function).
    status4, _ = asyncio.run(call(HDRS + [AUTH], INIT, path="/api/index"))
    check("path normalized", status4 == 200, f"status {status4}")

    # 7. No stored session gives a clear error, not a hang or a browser attempt.
    try:
        index.get_mfp_client_remote()
        check("no-session path errors", False, "did not raise")
    except RuntimeError as exc:
        check("no-session path errors", "No MyFitnessPal session is stored" in str(exc))
    except Exception as exc:
        check("no-session path errors", False, f"wrong type: {type(exc).__name__}: {exc}")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
