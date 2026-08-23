# Running this on Vercel

The stock server runs on your Mac, as a program Claude Code starts and talks to
directly. That means it only exists while a Claude Code session is open on that
machine, which is why logging a meal from a phone or a web session fails.

These files run the same server on Vercel instead, so every Claude surface
reaches one copy.

## What had to change, and why

**Only a stored session works.** The server documents four ways to
authenticate. Three of them need the Mac (a file in your home directory, a
Chromium browser's cookie database, `browser_cookie3`), and the fourth,
`MFP_USERNAME` and `MFP_PASSWORD`, is broken upstream: MyFitnessPal moved to
NextAuth, and `authenticate_with_credentials` in `server.py` says so in a
comment and raises. So the hosted copy reads one session from a small key-value
store, and `scripts/push_cookie.py` publishes it from the Mac.

**The session is written back after every call.** The stock server saves a
session only after a fresh browser or credential login, never after
successfully using a stored one. If MyFitnessPal refreshes the expiry on use,
that refresh was being thrown away. Writing it back is what lets the hosted copy
stay alive on its own.

**The transport is built per request.** FastMCP's `streamable_http_app()`
starts its task group in the ASGI lifespan, and serverless platforms never run
lifespan events, so every request would fail with "Task group is not
initialized." Running the manager by hand once does not fix it either, because
`run()` refuses a second call on the same instance and a warm container serves
many requests from one process. `api/index.py` therefore builds a fresh session
manager per request, which is what stateless mode does internally anyway.

`server.py` is not edited, so upstream fixes still merge cleanly.

## Setup

1. **Add a Redis store.** Vercel dashboard, Storage, Upstash Redis. The free
   tier is far more than this needs.

2. **Set four environment variables** on the project:

   | Name | Value |
   |---|---|
   | `MCP_AUTH_TOKEN` | any long random string you generate |
   | `UPSTASH_REDIS_REST_URL` | from the store's page |
   | `UPSTASH_REDIS_REST_TOKEN` | from the store's page |
   | `MFP_COOKIE_KEY` | optional, defaults to `mfp:cookies` |

   Without `MCP_AUTH_TOKEN` the endpoint refuses every request rather than
   serving an open URL that can write to a food diary.

3. **Deploy.**

4. **Publish a session**, from the Mac, logged into myfitnesspal.com in Chrome,
   Arc, Brave, or Edge:

   ```
   export UPSTASH_REDIS_REST_URL=...
   export UPSTASH_REDIS_REST_TOKEN=...
   python scripts/push_cookie.py
   ```

   It prefers the session the local connector already has, falls back to the
   browser, tests whichever it finds against MyFitnessPal before publishing, and
   refuses to publish one that does not work.

5. **Register the connector** in claude.ai settings as a custom connector:

   - URL: `https://<your-deployment>.vercel.app/`
   - Header: `Authorization: Bearer <MCP_AUTH_TOKEN>`

## Re-publishing a session

If the hosted server ever answers "The stored MyFitnessPal session was
rejected", run step 4 again. How often that happens is not yet known: it depends
on whether MyFitnessPal rolls its session expiry forward on use, which the
write-back above is designed to take advantage of. Watch the `saved_at` stamp in
the store to find out.

## Tests

`tests/test_vercel_wiring.py` covers everything `api/index.py` owns: the auth
branches, a real `tools/list` over HTTP, a second request in the same process,
path normalization, and the no-session error path. It does not touch
MyFitnessPal, so the live calls stay unproven until a session is published and
a meal is actually logged through the deployed URL.

```
pip install -r requirements.txt
python tests/test_vercel_wiring.py
```
