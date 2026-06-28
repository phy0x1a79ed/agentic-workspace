"""aiohttp REST + WebSocket server for the mira API daemon.

Runs ON mira. Exposes a clean per-platform API over the awm-network so the awm
``social`` connectors can act as a thin client:

    GET  /v1/health                      → per-platform target liveness
    GET  /v1/{platform}/identity         → who the session is logged in as
    GET  /v1/{platform}/channels         → channels/conversations visible
    GET  /v1/{platform}/messages?channel=&limit=&before=  → history (before = page
                                                           backwards; Slack only)
    POST /v1/{platform}/send  {channel,text,thread?}
    GET  /v1/events                      → WS; pushes inbound {type:"message", …}

Every route requires ``Authorization: Bearer <token>``. The daemon polls each
platform on mira (the **inbound watcher**) and pushes new messages to all WS
clients, so awm-side clients never poll. One :class:`CDPPage` per platform
(serialised) drives the logged-in Opera tab via in-page fetch.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from aiohttp import web

from mira_api import drivers as drivers_mod
from mira_api.cdp import CDPPage

log = logging.getLogger("mira_api.server")

DEFAULT_POLL_INTERVAL_S = 15.0
WATCH_HISTORY_LIMIT = 50


class Platform:
    """Live state for one platform: its CDP page, driver, and watcher cursors."""

    def __init__(self, name: str, page: CDPPage, driver: drivers_mod.Driver) -> None:
        self.name = name
        self.page = page
        self.driver = driver
        self.self_id = ""          # own user id, to drop echoes of our own sends
        self.baseline: dict[str, str] = {}  # channel_id -> last-seen marker
        self.seen: set[str] = set()         # process-local message_id dedupe
        self.unreadable: set[str] = set()   # convs whose history endpoint 404s


class MiraAPI:
    def __init__(
        self,
        cdp_http_base: str,
        platforms: list[str],
        auth_token: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._cdp_http_base = cdp_http_base
        self._platform_names = platforms
        self._auth_token = auth_token
        self._poll_interval = poll_interval
        self._platforms: dict[str, Platform] = {}
        self._clients: set[web.WebSocketResponse] = set()
        self._watchers: list[asyncio.Task] = []

    # -- setup --------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_mw])
        app.router.add_get("/v1/health", self._h_health)
        app.router.add_get("/v1/events", self._h_events)
        app.router.add_get("/v1/{platform}/identity", self._h_identity)
        app.router.add_get("/v1/{platform}/channels", self._h_channels)
        app.router.add_get("/v1/{platform}/messages", self._h_messages)
        app.router.add_post("/v1/{platform}/send", self._h_send)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, app: web.Application) -> None:
        for name in self._platform_names:
            cls = drivers_mod.REGISTRY.get(name)
            if cls is None:
                log.error("unknown platform %r; skipping", name)
                continue
            page = CDPPage(self._cdp_http_base, cls.target_needle)
            self._platforms[name] = Platform(name, page, cls(page))
            self._watchers.append(asyncio.create_task(self._watch(name)))
            log.info("platform %s initialised (needle %r)", name, cls.target_needle)

    async def _on_cleanup(self, app: web.Application) -> None:
        for t in self._watchers:
            t.cancel()
        for ws in list(self._clients):
            await ws.close()
        for p in self._platforms.values():
            await p.page.close()

    # -- auth ---------------------------------------------------------------

    @web.middleware
    async def _auth_mw(self, request: web.Request, handler) -> web.StreamResponse:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, self._auth_token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def _platform(self, request: web.Request) -> Platform:
        name = request.match_info["platform"]
        p = self._platforms.get(name)
        if p is None:
            raise web.HTTPNotFound(reason=f"platform {name!r} not configured")
        return p

    # -- REST handlers ------------------------------------------------------

    async def _h_health(self, request: web.Request) -> web.Response:
        out: dict[str, Any] = {"ok": True, "platforms": {}}
        for name, p in self._platforms.items():
            healthy = await p.page.healthy()
            out["platforms"][name] = {
                "target": p.page.target_url, "healthy": healthy,
                "self_id": p.self_id, "watching": len(p.baseline)}
        return web.json_response(out)

    async def _h_identity(self, request: web.Request) -> web.Response:
        p = self._platform(request)
        try:
            return web.json_response(await p.driver.identity())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

    async def _h_channels(self, request: web.Request) -> web.Response:
        p = self._platform(request)
        try:
            return web.json_response({"channels": await p.driver.list_channels()})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

    async def _h_messages(self, request: web.Request) -> web.Response:
        p = self._platform(request)
        channel = request.query.get("channel", "")
        if not channel:
            return web.json_response({"error": "channel is required"}, status=400)
        limit = int(request.query.get("limit", "20") or 20)
        before = request.query.get("before") or None
        try:
            msgs = await p.driver.fetch_messages(channel, None, limit, before=before)
            return web.json_response({"messages": msgs})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

    async def _h_send(self, request: web.Request) -> web.Response:
        p = self._platform(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON body"}, status=400)
        channel, text = body.get("channel"), body.get("text")
        if not channel or text is None:
            return web.json_response(
                {"error": "channel and text are required"}, status=400)
        try:
            sent = await p.driver.send(channel, text, body.get("thread"))
            return web.json_response({"ok": True, **sent})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

    # -- WebSocket fan-out --------------------------------------------------

    async def _h_events(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("ws client connected (%d total)", len(self._clients))
        try:
            await ws.send_json({"type": "ready",
                                "platforms": list(self._platforms)})
            async for _msg in ws:  # drain client frames (keepalive/subscribe)
                pass
        finally:
            self._clients.discard(ws)
            log.info("ws client disconnected (%d total)", len(self._clients))
        return ws

    async def _broadcast(self, payload: dict) -> None:
        for ws in list(self._clients):
            if ws.closed:
                self._clients.discard(ws)
                continue
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                self._clients.discard(ws)

    # -- inbound watcher ----------------------------------------------------

    async def _watch(self, name: str) -> None:
        """Poll one platform and push new inbound messages to WS clients.

        Mirrors the connectors' baseline-then-delta discipline: a conversation
        seen for the first time is baselined to its latest marker and NOT
        replayed; only strictly-newer messages on known conversations emit. Own
        messages (sends echoed back) and non-text events are dropped.
        """
        p = self._platforms[name]
        backoff = 1.0
        first = True
        while True:
            try:
                if not p.self_id:
                    ident = await p.driver.identity()
                    p.self_id = ident.get("id", "") or ""
                convs = await p.driver.list_conversations()
                if first:
                    log.info("%s watcher: %d conversations, poll %.0fs",
                             name, len(convs), self._poll_interval)
                    first = False
                for conv in convs:
                    cid = conv.get("id", "")
                    if cid in p.unreadable:
                        continue
                    try:
                        await self._poll_conversation(p, conv)
                    except Exception as exc:  # noqa: BLE001
                        # One conversation we can't read (e.g. a Teams team
                        # channel, served by a different backend than the ng.msg
                        # chat service) must never stall the whole poll — mark it
                        # unreadable so we stop retrying it every cycle.
                        if cid:
                            p.unreadable.add(cid)
                        log.debug("%s skip conversation %s: %s", name, cid, exc)
                backoff = 1.0
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("%s watcher error (%s); retry in %.1fs",
                            name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _poll_conversation(self, p: Platform, conv: dict) -> None:
        cid = conv.get("id", "")
        if not cid:
            return
        if cid not in p.baseline:
            latest = await p.driver.fetch_messages(cid, None, 1)
            p.baseline[cid] = latest[0]["marker"] if latest else ""
            return
        msgs = await p.driver.fetch_messages(cid, p.baseline[cid], WATCH_HISTORY_LIMIT)
        newest = p.baseline[cid]
        # platform returns newest-first; emit oldest-first
        for m in sorted(msgs, key=lambda x: x.get("marker", "")):
            marker = m.get("marker", "")
            if not marker or marker <= p.baseline[cid]:
                continue
            if marker > newest:
                newest = marker
            if m.get("sender_id") and m["sender_id"] == p.self_id:
                continue  # echo of our own send
            if not m.get("text"):
                continue
            mid = m.get("message_id", "")
            if mid and mid in p.seen:
                continue
            if mid:
                p.seen.add(mid)
            m["channel_name"] = m.get("channel_name") or conv.get("name", "")
            await self._broadcast({"type": "message", "message": m})
        p.baseline[cid] = newest
