"""An MCP endpoint over HTTP, exposing a curated slice of awm's verbs.

The workbench takes custom connectors two ways, and the choice is forced by
where each one runs. A *local command* connector is a stdio server that runs
**inside the analysis sandbox**, under the same network allowlist as Claude's
code — so pointing it at ``awm-mcp`` would put a process that needs loopback
HTTP to the gateway, and that reads the workspace's env file, inside a
bubblewrap jail designed to prevent exactly that. A *remote* connector is
called by the app itself, outside the sandbox. That is the one that can work.

awm has no MCP-over-HTTP transport — ``awm-mcp`` stdio is the only one — so
this module is it: a minimal JSON-RPC endpoint speaking MCP's ``initialize`` /
``tools/list`` / ``tools/call``, translating each call into the gateway's own
``GET /tools`` and ``POST /invoke``. It registers as a ``kind=url`` mount, so
httpsfront gives it TLS and the edge session and there is no second listener to
secure.

**The allowlist is the point.** awm has no per-caller mode and no read-only
credential: anything that can reach the loopback bus gets the whole surface —
``ssh``, ``compute``, ``social``, gateway control, agent write verbs. The
``agent`` domain's own verb gate says in a comment that it is defence in depth
and *not* a trust boundary, because the identity it keys on is spoofable. So
handing this connector "all of awm" would not be a shortcut, it would be the
absence of a decision. What ships instead is an explicit list, enforced on
``tools/call`` and not merely on ``tools/list`` — hiding a tool from a listing
is not a control, since the caller composes the request either way.

Widening it is a config edit and a review, which is the correct cost.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("awm.claude_science.mcp_bridge")

#: Prefix this bridge is mounted at on the gateway.
MOUNT_PREFIX = os.environ.get("CLAUDE_SCIENCE_MCP_PREFIX", "/claude-science/mcp")

#: MCP protocol revision we negotiate. Echoed back on `initialize`.
PROTOCOL_VERSION = "2025-06-18"

#: The curated surface, as ``domain -> (verb, …)``. Read and query verbs only:
#: the workbench is a place to analyse things, so what it needs from awm is the
#: ability to *find* them. Deliberately conservative — a verb that turns out to
#: be wanted is one line and a review; a verb that turns out to be dangerous is
#: an incident.
#:
#: Excluded on purpose, and worth naming so the omissions read as decisions:
#:   ssh / vpn / 2fa / social  — act on the fleet's credentials and channels
#:   agent / orch / scope write — spawn work or mutate branches
#:   services / gateway control — can stop the very service serving this
#:   compute                    — schedules jobs on shared machines
DEFAULT_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "scope": ("search", "resolve", "data_status", "fetch"),
    "project": ("search",),
    "notes": ("list", "get", "search"),
    "drawio": ("list", "info", "read", "cells", "export", "url", "view_url"),
    "graphify": ("find", "refs", "query", "path", "affected"),
    "dvc": ("coverage",),
    "artifact": ("list", "get"),
    "precedence": ("search",),
}


def _load_allowlist() -> dict[str, tuple[str, ...]]:
    """Allow the deployment to narrow or widen the surface.

    ``CLAUDE_SCIENCE_MCP_ALLOW`` is a JSON object of the same shape. Set to
    ``{}`` to expose nothing at all, which is a legitimate way to run the
    workbench with the connector wired but inert.
    """
    raw = os.environ.get("CLAUDE_SCIENCE_MCP_ALLOW")
    if not raw:
        return dict(DEFAULT_ALLOWLIST)
    try:
        parsed = json.loads(raw)
        return {str(d): tuple(str(v) for v in verbs)
                for d, verbs in parsed.items()}
    except Exception:  # noqa: BLE001 — a broken allowlist must not widen it
        log.exception("CLAUDE_SCIENCE_MCP_ALLOW is unparseable; "
                      "falling back to the built-in list")
        return dict(DEFAULT_ALLOWLIST)


ALLOW = _load_allowlist()

#: MCP tool names are flat, so a domain/verb pair is joined with a separator
#: that cannot occur in either half.
_SEP = "__"


def tool_name(domain: str, verb: str) -> str:
    return f"awm{_SEP}{domain}{_SEP}{verb}"


def split_tool(name: str) -> tuple[str, str] | None:
    parts = name.split(_SEP)
    if len(parts) != 3 or parts[0] != "awm":
        return None
    return parts[1], parts[2]


def is_allowed(domain: str, verb: str) -> bool:
    return verb in ALLOW.get(domain, ())


def _hub_url() -> str:
    return os.environ.get("AWM_HUB_URL", "http://127.0.0.1:7819/").rstrip("/")


async def _catalog(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """The allowlisted verbs, described from the gateway's live catalog.

    Descriptions and parameter schemas come from the catalog rather than being
    restated here, so a verb's documentation cannot drift from the service that
    implements it. Anything the catalog does not know about is dropped: an
    allowlist entry for a domain that is not running should be invisible, not a
    tool that always errors.
    """
    resp = await client.get(f"{_hub_url()}/tools", timeout=30.0)
    resp.raise_for_status()
    payload = resp.json()
    entries = payload.get("tools") if isinstance(payload, dict) else payload
    by_name = {e.get("name"): e for e in (entries or []) if e.get("name")}

    # Look each allowlisted pair up by its exact catalog name rather than
    # parsing names apart. Splitting `<domain>_<verb>` is ambiguous in both
    # directions — verbs contain underscores (`data_status`) and a hyphenated
    # domain arrives underscored (`rlm-browser` → `rlm_browser_*`) — so a parse
    # would either drop real tools or, worse, resolve one domain's name onto
    # another's. Iterating the allowlist also means an entry for a domain that
    # is not running is simply absent, not a tool that always errors.
    tools: list[dict[str, Any]] = []
    for domain, verbs in ALLOW.items():
        for verb in verbs:
            entry = by_name.get(f"{domain}_{verb}")
            if entry is None:
                continue
            tools.append({
                "name": tool_name(domain, verb),
                "description": entry.get("description") or "",
                "inputSchema": entry.get("inputSchema")
                or {"type": "object", "properties": {}},
            })
    return tools


async def _invoke(client: httpx.AsyncClient, domain: str, verb: str,
                  args: dict[str, Any]) -> Any:
    resp = await client.post(
        f"{_hub_url()}/invoke",
        json={"name": f"{domain}_{verb}", "args": args or {}},
        timeout=300.0,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("result", body)


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _rpc_ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def handle_rpc(message: dict[str, Any],
                     client: httpx.AsyncClient) -> dict[str, Any] | None:
    """One JSON-RPC request in, one response out (or None for a notification)."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    if req_id is None:
        # A notification (`notifications/initialized`, say) — nothing to answer.
        return None

    if method == "initialize":
        return _rpc_ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "awm", "version": "0.1.0"},
        })

    if method == "ping":
        return _rpc_ok(req_id, {})

    if method == "tools/list":
        try:
            return _rpc_ok(req_id, {"tools": await _catalog(client)})
        except Exception as exc:  # noqa: BLE001
            log.exception("tools/list failed")
            return _rpc_error(req_id, -32603, f"catalog unavailable: {exc}")

    if method == "tools/call":
        name = str(params.get("name") or "")
        pair = split_tool(name)
        if pair is None:
            return _rpc_error(req_id, -32602, f"unknown tool {name!r}")
        domain, verb = pair
        # Enforced here, not only in the listing: a caller composes the request
        # itself, so a tool that is merely hidden is a tool that is available.
        if not is_allowed(domain, verb):
            return _rpc_error(
                req_id, -32601,
                f"{domain}.{verb} is not on this connector's allowlist",
            )
        try:
            result = await _invoke(client, domain, verb,
                                   params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 — surface, never crash the bridge
            log.warning("tools/call %s.%s failed: %s", domain, verb, exc)
            return _rpc_ok(req_id, {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            })
        text = result if isinstance(result, str) else json.dumps(result, indent=2)
        return _rpc_ok(req_id, {"content": [{"type": "text", "text": text}]})

    return _rpc_error(req_id, -32601, f"method not supported: {method}")


# ---------------------------------------------------------------------------
# The listener and its gateway mount
# ---------------------------------------------------------------------------

_STATE: dict[str, Any] = {
    "listening_port": None, "mounted": False, "prefix": MOUNT_PREFIX,
    "allowlist": {d: list(v) for d, v in ALLOW.items()},
    "tool_count": sum(len(v) for v in ALLOW.values()),
    "error": None,
}


def status() -> dict:
    return dict(_STATE)


def build_app():
    """A Starlette app speaking MCP over HTTP POST.

    One catch-all route: the gateway preserves the mount prefix when it
    proxies, so requests arrive here as ``/claude-science/mcp`` rather than
    ``/``, and a client that appends its own path segment should still land.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    # One client for the life of the process, held in this closure rather than
    # built by a lifespan handler and stashed on ``app.state``. The listener
    # runs with uvicorn's lifespan protocol *off* (see ``serve_and_mount``), so
    # a lifespan-built client is simply never created — and the failure lands
    # far from the cause, as ``KeyError: 'client'`` on the first call, with GET
    # /status still answering 200 because it never touches the client. A closure
    # cannot be half-wired.
    client = httpx.AsyncClient()

    async def _endpoint(request: Request) -> Response:
        try:
            message = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(_rpc_error(None, -32700, "parse error"),
                                status_code=400)
        # A batch is a list; MCP clients rarely send one, but answering it is
        # three lines and refusing it is a support question.
        if isinstance(message, list):
            out = [r for r in
                   [await handle_rpc(m, client) for m in message] if r]
            return JSONResponse(out) if out else Response(status_code=202)
        reply = await handle_rpc(message, client)
        if reply is None:
            return Response(status_code=202)  # notification, nothing to say
        return JSONResponse(reply)

    async def _health(request: Request) -> Response:
        return JSONResponse(status())

    return Starlette(
        routes=[
            Route("/{path:path}", _health, methods=["GET"]),
            Route("/{path:path}", _endpoint, methods=["POST"]),
        ],
    )


async def serve_and_mount() -> None:
    """Run the loopback listener and keep it mounted on the gateway.

    Two things in one coroutine because they are one fact: the mount points at
    a port, so re-registering after a gateway restart has to advertise the port
    we are actually listening on. Runs as a background task off the adapter's
    ``on_start`` and never returns.

    The listener binds an OS-chosen loopback port. The stable address is the
    gateway prefix, which is what the connector is configured with — reachable
    unauthenticated on the gateway's own loopback, and through ``httpsfront``
    with the edge session, without this module knowing about either.
    """
    import asyncio

    import uvicorn
    import websockets

    config = uvicorn.Config(
        build_app(), host="127.0.0.1", port=0, log_level="warning",
        # The app deliberately has no lifespan handlers (build_app holds its
        # client in a closure), and leaving the protocol on means every teardown
        # logs a CancelledError traceback at ERROR from uvicorn's lifespan
        # receive() — recurring noise that reads like a fault in this service
        # and is not one. Keep the two facts together: turning this off while
        # the app relies on a lifespan is what broke every call once already.
        lifespan="off",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    # Wait for the bind so the mount advertises a port that exists.
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    if not server.started:
        _STATE["error"] = "listener failed to start"
        log.error("mcp bridge: listener did not start")
        return
    port = server.servers[0].sockets[0].getsockname()[1]
    _STATE.update({"listening_port": port, "error": None})
    log.info("mcp bridge listening on 127.0.0.1:%d (%d tools allowlisted)",
             port, _STATE["tool_count"])

    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub_url:
        _STATE["error"] = "AWM_HUB_URL unset; not mounted"
        log.error("mcp bridge: AWM_HUB_URL not set; cannot register the mount")
        await task
        return
    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")

    backoff = 1.0
    while True:
        try:
            async with httpx.AsyncClient(verify=False, timeout=15) as cli:
                r = await cli.post(f"{hub_url}/hub/register", json={
                    "name": "claude-science-mcp",
                    "prefix": MOUNT_PREFIX,
                    "url": f"http://127.0.0.1:{port}",
                })
                r.raise_for_status()
            body = r.json()
            _STATE["mounted"] = True
            log.info("mcp bridge mounted: %s → 127.0.0.1:%d (id=%s)",
                     MOUNT_PREFIX, port, body.get("service_id"))
            backoff = 1.0
            async with websockets.connect(
                f"{ws_base}{body['lease_ws_path']}",
                max_size=None, open_timeout=10,
            ) as ws:
                # First frame is "ready"; then hold the socket. Registry records
                # are in-memory, so a gateway restart drops the mount and the
                # loop below puts it back.
                async for _ in ws:
                    pass
        except Exception as exc:  # noqa: BLE001 — stay up across any fault
            log.warning("mcp bridge mount lost (%s); retrying in %.1fs",
                        exc, backoff)
        finally:
            _STATE["mounted"] = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
