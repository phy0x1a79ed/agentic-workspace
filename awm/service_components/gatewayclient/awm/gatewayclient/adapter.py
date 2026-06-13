"""Reusable service-side adapter — the boot/register/serve loop every feature
service runs to expose its functions through the gateway.

Out-of-process is the gateway's only service model: the gateway spawns each
service as its own process (PID-journaled, control-WS lease as the liveness
signal, 10s reconnect+respawn on silence). A service therefore needs a small
client that POSTs ``/hub/service/register``, opens ``WS
/hub/service/control/<id>``, sends a ``ready`` frame carrying its api manifest,
then answers inbound ``call`` / ``notify`` / ``session.open`` envelopes.

Before this module, every service hand-rolled that ~250-line loop (see the
``tts`` / ``ptt`` ``backend/hub_adapter.py``). ``ServiceAdapter`` factors the
boilerplate out so a feature service's adapter is just: build a manifest +
a ``{fn: callable}`` dispatch map, then ``await ServiceAdapter(...).run()``.

The companion ``call`` / ``RefCache`` in this package is the *other* direction
(this service calling another); this module is *being called*.

Example
-------
    from awm.gatewayclient import ServiceAdapter

    MANIFEST = {
        "functions": [
            {"name": "list_operators"},
            {"name": "add_operator", "params": [
                {"name": "discord_user_id", "type": "string", "required": True},
                {"name": "awm_user", "type": "string", "required": True},
            ]},
        ],
        "emitters": [],
        "sessions": [],
    }

    HANDLERS = {
        "list_operators": lambda args: dao.list_operators(),
        "add_operator": lambda args: dao.add_operator(
            args["discord_user_id"], args["awm_user"]),
    }

    async def main():
        await ServiceAdapter("discord", MANIFEST, HANDLERS,
                             on_start=dao.init).run()
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import ssl
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

log = logging.getLogger("awm.gatewayclient.adapter")

# How long (seconds) the run loop keeps retrying after losing the control WS
# before concluding the gateway is gone for good and exiting cleanly. Matches
# the gateway's own ``_RECONNECT_WINDOW_S`` so a hard-killed gateway leaves no
# orphaned services behind (see the lifecycle plan: T2). Override via env.
_RECONNECT_DEADLINE_S = float(os.environ.get("AWM_RECONNECT_DEADLINE_S", "10.0"))


class GiveUp(Exception):
    """Sentinel: the service has been told to stand down (or the gateway is
    gone for good). Raising it out of ``_register``/``_serve`` makes ``run()``
    return cleanly — the process exits 0 with no further retry."""

# A handler takes the call's ``args`` dict and may optionally take the caller
# identity ``as_`` as a second positional arg. It may be sync or async; sync
# handlers run in a worker thread so a blocking DAO call never stalls the loop.
Handler = Callable[..., Any]


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ws_base(hub_url: str) -> str:
    return hub_url.replace("https://", "wss://").replace("http://", "ws://")


@dataclass
class SessionContext:
    """Everything a ``session.open`` handler needs to run a direct session.

    ``open_bridge()`` opens the upstream side of the hub bridge so the handler
    can byte-relay frames (the PCM-audio case for tts/ptt). Most feature
    services have no sessions and never use this.
    """
    hub_url: str
    service_id: str
    session_id: str
    bridge_id: str | None
    init: dict[str, Any]

    async def open_bridge(self) -> Any:
        if not self.bridge_id:
            raise RuntimeError("session has no bridge_id; not a direct session")
        ws_base = _ws_base(self.hub_url)
        return await websockets.connect(
            f"{ws_base}/hub/service/bridge/{self.service_id}/{self.bridge_id}",
            ssl=_ssl_ctx() if ws_base.startswith("wss://") else None,
            max_size=None,
            open_timeout=10,
        )


class ServiceAdapter:
    """Drives one service's register → ready → serve → reconnect lifecycle.

    Parameters
    ----------
    name:
        Service name (also the ``/svc/<name>`` prefix). ``AWM_SERVICE_NAME``
        from env overrides it when the hub spawned the process.
    manifest:
        The ``ready.api`` manifest — ``{"functions": [...], "emitters": [...],
        "sessions": [...]}``. Only ``functions[]`` is projected into the
        catalog today; declare ``sessions``/``emitters`` for the bridge paths.
    handlers:
        ``{fn_name: callable}``. Each callable receives the call ``args`` dict;
        if it declares a second positional parameter it also receives the
        caller identity ``as_``. Sync callables run in a thread; async
        callables are awaited. The return value is JSON-serialized as the
        reply ``result`` (return ``None`` for fire-and-forget / no payload).
    session_handlers:
        Optional ``{session_kind: async callable(SessionContext)}`` for
        services that expose direct sessions (tts/ptt PCM bridges).
    on_start:
        Optional sync-or-async zero-arg callable run once before the first
        connect — the place to call ``init_service_db`` so the service's own
        DB exists before it serves.
    start_cmd:
        Argv the hub uses to respawn the service after a silence eviction.
        Defaults to ``["bash", "run.sh"]`` (the convention every service's
        ``run.sh`` honours). This matches what filesystem discovery records, so
        a self-registered service and a bootstrap-spawned one are identical.
    """

    def __init__(
        self,
        name: str,
        manifest: dict[str, Any],
        handlers: dict[str, Handler],
        *,
        session_handlers: dict[str, Callable[[SessionContext], Awaitable[None]]]
        | None = None,
        on_start: Callable[[], Any] | None = None,
        start_cmd: list[str] | None = None,
    ) -> None:
        self.name = os.environ.get("AWM_SERVICE_NAME") or name
        self.manifest = manifest
        self.handlers = handlers
        self.session_handlers = session_handlers or {}
        self.on_start = on_start
        self.start_cmd = start_cmd or ["bash", "run.sh"]
        # Last monotonic time the control WS was confirmed up; drives the
        # reconnect-deadline give-up in run(). Updated in run() and _serve.
        self._last_up = monotonic()

    # -- dispatch ----------------------------------------------------------

    async def _dispatch(self, fn: str | None, args: Any, as_: str | None) -> Any:
        handler = self.handlers.get(fn or "")
        if handler is None:
            raise RuntimeError(f"unknown function {fn!r}")
        args = args or {}
        # Pass as_ only if the handler asked for a second positional arg.
        try:
            nparams = len(inspect.signature(handler).parameters)
        except (TypeError, ValueError):
            nparams = 1
        call_args = (args, as_) if nparams >= 2 else (args,)
        if inspect.iscoroutinefunction(handler):
            return await handler(*call_args)
        result = await asyncio.to_thread(handler, *call_args)
        # A sync handler may itself return a coroutine (rare); await it.
        if inspect.isawaitable(result):
            result = await result
        return result

    # -- control-WS receive loop ------------------------------------------

    async def _serve(self, hub_url: str, sid: str) -> None:
        ws_base = _ws_base(hub_url)
        ws_url = f"{ws_base}/hub/service/control/{sid}"
        try:
            async with websockets.connect(
                ws_url,
                ssl=_ssl_ctx() if ws_base.startswith("wss://") else None,
                max_size=None,
                open_timeout=10,
            ) as ws:
                await ws.send(json.dumps({"kind": "ready", "api": self.manifest}))
                log.info("%s: control WS open, ready sent", self.name)
                try:
                    async for raw in ws:
                        try:
                            env = json.loads(raw) if isinstance(raw, str) else None
                        except json.JSONDecodeError:
                            continue
                        if env is None:
                            continue
                        kind = env.get("kind")
                        if kind == "call":
                            asyncio.create_task(self._handle_call(ws, env))
                        elif kind == "notify":
                            asyncio.create_task(self._handle_notify(env))
                        elif kind == "session.open":
                            asyncio.create_task(self._handle_session_open(
                                ws, hub_url, sid, env))
                        elif kind == "shutdown":
                            # The gateway's graceful stand-down (T4). Exit the
                            # run loop cleanly rather than reconnect-bouncing.
                            log.info("%s: shutdown frame received; standing down",
                                     self.name)
                            raise GiveUp("gateway requested shutdown")
                        else:
                            log.debug("%s: ignored inbound kind=%s",
                                      self.name, kind)
                finally:
                    # The control WS was actually established this round; record
                    # when it ended so run()'s give-up deadline counts the
                    # *retry* window from the disconnect, not from process start
                    # (a brief blip after hours of uptime must still retry).
                    self._last_up = monotonic()
        except ConnectionClosed as exc:
            # The hub closed the control WS with a terminal code: 4409 = the
            # lease is already held by a live incumbent, 4404 = the service_id
            # is unknown. Either way this instance must give up; any other code
            # (e.g. 1006 abnormal, 1012 going-away) is transient → re-raise so
            # run() retries within the deadline.
            code = exc.rcvd.code if exc.rcvd is not None else None
            if code in (4409, 4404):
                raise GiveUp(f"control WS closed with code {code}") from exc
            raise
        except InvalidStatus as exc:
            # The WS upgrade itself was rejected with 409 → a live incumbent
            # holds the slot; give up.
            if getattr(exc.response, "status_code", None) == 409:
                raise GiveUp("control WS upgrade rejected (409)") from exc
            raise

    async def _handle_call(self, ws: Any, env: dict[str, Any]) -> None:
        try:
            result = await self._dispatch(
                env.get("fn"), env.get("args"), env.get("as"))
            await ws.send(json.dumps({
                "kind": "reply", "id": env.get("id"),
                "ok": True, "result": result,
            }))
        except Exception as exc:  # report the error back to the caller
            log.warning("%s: call %s failed: %s",
                        self.name, env.get("fn"), exc)
            await ws.send(json.dumps({
                "kind": "reply", "id": env.get("id"),
                "ok": False, "error": str(exc),
            }))

    async def _handle_notify(self, env: dict[str, Any]) -> None:
        try:
            await self._dispatch(env.get("fn"), env.get("args"), env.get("as"))
        except Exception as exc:
            log.warning("%s: notify %s failed: %s",
                        self.name, env.get("fn"), exc)

    async def _handle_session_open(self, ws: Any, hub_url: str,
                                   sid: str, env: dict[str, Any]) -> None:
        session_id = env.get("session_id")
        kind = env.get("session_kind") or env.get("kind_arg") or "call"
        handler = self.session_handlers.get(kind)
        if handler is None:
            await ws.send(json.dumps({
                "kind": "session.opened", "session_id": session_id,
                "ok": False, "error": f"no session handler for kind {kind!r}",
            }))
            return
        try:
            await ws.send(json.dumps({
                "kind": "session.opened", "session_id": session_id, "ok": True,
            }))
            ctx = SessionContext(
                hub_url=hub_url, service_id=sid,
                session_id=session_id, bridge_id=env.get("bridge_id"),
                init=env.get("init") or {},
            )
            await handler(ctx)
        except Exception as exc:
            log.warning("%s: session open failed: %s", self.name, exc)
            try:
                await ws.send(json.dumps({
                    "kind": "session.opened", "session_id": session_id,
                    "ok": False, "error": str(exc),
                }))
            except Exception:
                pass

    # -- registration + reconnect loop ------------------------------------

    async def _register(self, hub_url: str) -> str:
        # An overlay carries a UNIQUE name (registry keys records by name) but
        # the BASE's prefix, so it stacks on /svc/<base>. `awm dev shadow` sets
        # AWM_SERVICE_NAME=<unique> + AWM_SERVICE_PREFIX=/svc/<base> +
        # AWM_SERVICE_OVERLAY=1. A normal service leaves prefix to default.
        prefix = os.environ.get("AWM_SERVICE_PREFIX") or f"/svc/{self.name}"
        payload = {
            "name": self.name,
            "prefix": prefix,
            "pid": os.getpid(),
            "start": self.start_cmd,
            "cwd": os.getcwd(),
            "overlay": bool(os.environ.get("AWM_SERVICE_OVERLAY")),
        }
        async with httpx.AsyncClient(verify=False, timeout=15) as cli:
            r = await cli.post(f"{hub_url}/hub/service/register", json=payload)
            if r.status_code == 409:
                # A live instance already holds this name's lease (T3). Stand
                # down — the incumbent keeps running. Other non-2xx stay
                # retryable via raise_for_status below.
                raise GiveUp(f"register rejected (409): {r.text}")
            r.raise_for_status()
        return r.json()["service_id"]

    async def run(self) -> None:
        """Register and serve forever, reconnecting with exponential backoff.

        Reads ``AWM_HUB_URL`` (required) and ``AWM_SERVICE_ID`` (set by the hub
        on respawn) from env. The registration handshake carries no auth — the
        gateway binds loopback-only.
        """
        hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
        sid = os.environ.get("AWM_SERVICE_ID", "")
        if not hub_url:
            raise RuntimeError("AWM_HUB_URL not set; cannot reach the gateway")

        if self.on_start is not None:
            res = self.on_start()
            if inspect.isawaitable(res):
                await res

        backoff = 1.0
        # ``_last_up`` tracks the last moment the control WS was confirmed up
        # (set in ``_serve``'s finally, which is reached only once the WS
        # actually connected). Initialised to now so a service that can never
        # reach the gateway still gives up after the deadline rather than
        # retrying forever.
        self._last_up = monotonic()
        while True:
            try:
                if not sid:
                    sid = await self._register(hub_url)
                    log.info("%s: registered service_id=%s", self.name, sid)
                await self._serve(hub_url, sid)
                # Clean return = the WS was up and the gateway closed it
                # without a terminal code; treat as a fresh disconnect.
                self._last_up = monotonic()
                backoff = 1.0
            except GiveUp as exc:
                # Told to stand down (duplicate reject / terminal close /
                # shutdown frame). Exit cleanly — no retry.
                log.info("%s: giving up: %s", self.name, exc)
                return
            except Exception as exc:
                if monotonic() - self._last_up > _RECONNECT_DEADLINE_S:
                    # The gateway has been unreachable past the deadline — it
                    # is gone for good (hard kill / idle os._exit, where its
                    # lifespan shutdown never ran). Stop rather than spin
                    # forever as an orphan.
                    log.warning(
                        "%s: gateway unreachable for >%.0fs; exiting",
                        self.name, _RECONNECT_DEADLINE_S)
                    return
                log.warning("%s: control WS lost (%s); retry in %.1fs",
                            self.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
