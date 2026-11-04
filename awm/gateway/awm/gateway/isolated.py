"""The door for isolated sessions (fleet.md R109): MCP over HTTP for a container session.

A container has no host pid, so it names itself by the caller token cch minted for it
(``X-Awm-Caller-Token``). The token's holder comes from ``harness-cc caller_of_token``,
and carries the session's whitelist. The door admits a verb only on that whitelist, only
on this node and only inside the session's own scope. It dispatches the catalog entry
itself, so none of ``/invoke``'s pid-header stamps can run for a container.

The Rust gateway's ``isolated.rs`` holds the same rules. Change both together.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from awm.config import node_name
from awm.gateway import catalog, mcp_caller

log = logging.getLogger("awm.gateway.isolated")

TOKEN_HEADER = "X-Awm-Caller-Token"
PROVIDER = "harness-cc_caller_of_token"
SESSION_KEY = "_caller_session_id"
LISTEN_ENV = "AWM_ISOLATED_LISTEN"
CACHE_S = 10.0

#: The reflection verbs that act on the caller's own session. Every other one reaches
#: another session, which an isolated session may never do (R109 item 2).
OWN_SESSION_VERBS = frozenset({"whoami", "compact", "message_self", "pending", "mode"})

#: The levels a token may carry. An isolated session is never trusted.
LEVELS = ("untrusted", "cooperative")


class Refused(PermissionError):
    pass


@dataclass(frozen=True)
class Holder:
    session_id: str
    project: str
    scope: str
    trust: str = "untrusted"
    name: str | None = None
    tools: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_answer(cls, answer: Any) -> Holder | None:
        """Read a provider answer. An unknown token, or one with no session or scope, is None."""
        if not isinstance(answer, dict) or answer.get("known") is not True:
            return None

        def text(key: str) -> str | None:
            v = answer.get(key)
            return v.strip() if isinstance(v, str) and v.strip() else None

        sid, project, scope = text("session_id"), text("project"), text("scope")
        if not (sid and project and scope):
            return None
        trust = text("trust")
        raw = answer.get("tools") if isinstance(answer.get("tools"), dict) else {}
        tools = {t: frozenset(v for v in verbs if isinstance(v, str))
                 for t, verbs in raw.items() if isinstance(verbs, list)}
        return cls(session_id=sid, project=project, scope=scope,
                   trust=trust if trust in LEVELS else LEVELS[0],
                   name=text("name"), tools=tools)

    @property
    def own_scope(self) -> str:
        return f"{self.project}/{self.scope}"

    def may_use(self, tool: str) -> bool:
        return _within_ceiling(tool, None) and tool in self.tools

    def may(self, tool: str, verb: str) -> bool:
        return _within_ceiling(tool, verb) and verb in self.tools.get(tool, ())


def _within_ceiling(tool: str, verb: str | None) -> bool:
    """What the door refuses whatever a token says: ssh (R109 item 1), the harness
    plugins, and every reflection verb that reaches another session (item 2)."""
    if tool == "ssh" or tool.startswith("harness-"):
        return False
    if tool == "reflection" and verb is not None:
        return verb in OWN_SESSION_VERBS
    return True


def pin(holder: Holder, declared: set[str], args: dict) -> dict:
    """Pin a call to the holder's own scope, then stamp who is calling.

    ``project``, ``scope`` and ``to_scope`` must name the session's own when sent. A
    verb that declares ``project`` or ``scope`` gets the session's value when absent.
    """
    out = {k: v for k, v in args.items() if not str(k).startswith("_caller")}
    own = holder.own_scope
    allowed = {"project": {holder.project}, "scope": {holder.scope, own},
               "to_scope": {holder.scope, own}}
    for key, ok in allowed.items():
        value = out.get(key)
        if value is None:
            continue
        if not (isinstance(value, str) and value.strip() in ok):
            raise Refused(f"an isolated session acts only in its own scope {own}; "
                          f"{key}={json.dumps(value)} is outside it")
    for key, value in (("project", holder.project), ("scope", holder.scope)):
        if key in declared and out.get(key) is None:
            out[key] = value
    out[mcp_caller.TRUST_KEY] = holder.trust
    out[mcp_caller.SCOPE_KEY] = own
    out[SESSION_KEY] = holder.session_id
    return out


# --- the token's holder ---------------------------------------------------------

_seen: dict[str, tuple[float, Holder]] = {}


async def _ask_provider(token: str) -> Any:
    return json.loads(await catalog.dispatch(PROVIDER, {"token": token}))


async def holder_of(token: str) -> Holder | None:
    """The session holding ``token``. A revoked token stops working within ``CACHE_S``."""
    now = time.monotonic()
    hit = _seen.get(token)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    holder = Holder.from_answer(await _ask_provider(token))
    for stale in [t for t, (at, _) in _seen.items() if now - at >= CACHE_S]:
        del _seen[stale]
    if holder is not None:
        _seen[token] = (now, holder)
    return holder


# --- MCP ------------------------------------------------------------------------


def _cut(holder: Holder) -> dict[str, list[dict[str, Any]]]:
    cat = catalog._domain_catalog()
    return {d: [e for e in entries if holder.may(d, e["verb"])]
            for d, entries in cat.items() if holder.may_use(d)}


def list_tools(holder: Holder) -> list[dict]:
    blurbs = catalog._domain_blurbs()
    return [catalog.Tool(name=d,
                         description=catalog._local_domain_description(d, entries, blurbs.get(d)),
                         inputSchema=catalog._domain_envelope(
                             [e["verb"] for e in entries], with_peer=False),
                         ).model_dump(by_alias=True)
            for d, entries in _cut(holder).items()]


async def call(holder: Holder, tool: str, envelope: Any) -> str:
    """A call from an isolated session: its whitelist, this node only, its own scope."""
    if not isinstance(envelope, dict):
        raise ValueError("'arguments' must be an object")
    verb = envelope.get("verb")
    peer = envelope.get("peer")
    if peer not in (None, "local", node_name()):
        raise Refused(f"an isolated session runs verbs on this node only, not on peer {peer!r}")
    if not holder.may_use(tool):
        raise Refused(f"tool {tool!r} is not on this isolated session's whitelist")
    inner = envelope.get("args") or {}
    if not isinstance(inner, dict):
        raise ValueError("'args' must be an object")
    cut = _cut(holder)
    if verb == catalog._DESCRIBE_VERB:
        allowed = [e["verb"] for e in cut.get(tool, [])]
        wanted = inner.get("verbs")
        if isinstance(wanted, list):
            allowed = [v for v in allowed if v in wanted]
        return catalog._serialize(catalog._describe_domain(tool, allowed or None, cut)
                                  if allowed else {"domain": tool, "verbs": []})
    if not (isinstance(verb, str) and holder.may(tool, verb)):
        raise Refused(f"{tool}.{verb} is not on this isolated session's whitelist")
    entry = next((e for e in cut.get(tool, []) if e["verb"] == verb), None)
    if entry is None:
        raise ValueError(f"Unknown verb {verb!r} for domain {tool!r}")
    declared = set((entry["tool"].inputSchema or {}).get("properties", {}))
    args = pin(holder, declared, inner)
    from awm.gateway import usage
    usage.record(tool, verb)
    return await catalog._run_entry(entry, args, None,
                                    unknown=f"Unknown verb {verb!r} for domain {tool!r}")


def _error_text(exc: Exception) -> str:
    status = {Refused: 403, catalog.ArgumentError: 400, ValueError: 404}
    code = next((c for t, c in status.items() if isinstance(exc, t)), 500)
    detail = getattr(exc, "payload", None) or str(exc)
    return f"Error {code}: {detail if isinstance(detail, str) else json.dumps(detail)}"


async def _message(holder: Holder, msg: Any) -> dict | None:
    if not isinstance(msg, dict) or "id" not in msg or not isinstance(msg.get("method"), str):
        return None
    mid, method = msg["id"], msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        result: Any = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "awm", "version": "isolated"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list_tools(holder)}
    elif method == "tools/call":
        try:
            text, is_error = await call(holder, str(params.get("name") or ""),
                                        params.get("arguments") or {}), False
        except Exception as exc:  # noqa: BLE001 - every failure goes back to the model
            if not isinstance(exc, (Refused, ValueError)):
                log.warning("isolated %s: %s failed: %s", holder.session_id,
                            params.get("name"), exc)
            text, is_error = _error_text(exc), True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method {method!r} not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


async def mcp_endpoint(request: Request) -> Response:
    """``POST /mcp``: MCP over streamable HTTP with JSON replies."""
    token = (request.headers.get(TOKEN_HEADER) or "").strip()
    if not token:
        return JSONResponse({"detail": f"{TOKEN_HEADER} is required"}, status_code=401)
    try:
        holder = await holder_of(token)
    except Exception as exc:  # noqa: BLE001 - no provider is no door
        log.warning("isolated door: token lookup failed: %s", exc)
        return JSONResponse({"detail": f"cannot check the caller token: {exc}"}, status_code=503)
    if holder is None:
        return JSONResponse({"detail": "unknown caller token"}, status_code=401)
    try:
        body = await request.json()
    except ValueError as exc:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": str(exc)}}, status_code=400)
    batch = isinstance(body, list)
    replies = [r for m in (body if batch else [body]) if (r := await _message(holder, m))]
    if not replies:
        return Response(status_code=202)
    return JSONResponse(replies if batch else replies[0])


async def no_stream(request: Request) -> Response:
    return Response("this MCP door answers POST only", status_code=405)


def mount(app: FastAPI) -> None:
    app.add_api_route("/mcp", mcp_endpoint, methods=["POST"], include_in_schema=False)
    app.add_api_route("/mcp", no_stream, methods=["GET", "DELETE"], include_in_schema=False)


def door_app() -> FastAPI:
    """The door's own listener: ``/mcp`` and nothing else, so a container that reaches
    it cannot reach ``/invoke`` or the hub."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    mount(app)
    return app


def listen_address() -> tuple[str, int] | None:
    raw = (os.environ.get(LISTEN_ENV) or "").strip()
    if not raw:
        return None
    host, _, port = raw.rpartition(":")
    return (host.strip("[]") or "127.0.0.1", int(port))


def door_server(host: str, port: int):
    """A uvicorn server for the door that leaves the main server's signal handlers alone."""
    import uvicorn

    class _Door(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    return _Door(uvicorn.Config(door_app(), host=host, port=port, log_level="info",
                                lifespan="off"))
