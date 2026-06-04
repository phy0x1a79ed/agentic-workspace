"""HTTP and WS forwarding to a registered service.

Two distinct surfaces share this module:

* **URL proxy** (``proxy_http`` / ``proxy_ws``) — ``kind="url"``
  registrations. The hub authenticates to the upstream service AS
  ITSELF (injects ``Authorization: Bearer <local-auth.token>`` +
  ``X-Awm-From: <self-peer-id>``), strips the user's bearer/cookie,
  preserves ``X-Awm-As``.
* **Service RPC translator** (``proxy_service_http``,
  ``proxy_service_emit_ws``, ``proxy_session_ws``) — ``kind="service"``
  registrations. Translates browser-side REST/WS calls into JSON
  envelopes on the service's persistent control WS (see
  ``awm/services/hub/rpc.py``); for direct sessions/emitters, allocates
  a bridge WS pair and byte-relays raw frames.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx
import websockets
from fastapi import Request, WebSocket
from starlette.responses import JSONResponse, Response, StreamingResponse

from awm.services import auth as auth_svc
from awm.services.hub import rpc
from awm.services.network.federation import _local_peer_id

log = logging.getLogger("awm.hub.proxy")

# Headers the hub MUST strip before forwarding — either set by us
# (auth) or hop-by-hop per RFC 7230 §6.1.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
}
_STRIP_REQUEST = _HOP_BY_HOP | {"authorization", "cookie", "host"}
_STRIP_RESPONSE = _HOP_BY_HOP


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

# One client per running event loop. Caching across loops fails because
# httpx.AsyncClient binds to the loop that created it (via anyio); under
# pytest's per-test loops or multi-server processes the same singleton
# would 500 once a different loop tries to use it.
_http_clients: dict[int, httpx.AsyncClient] = {}


def _client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    key = id(loop)
    cli = _http_clients.get(key)
    if cli is None:
        cli = httpx.AsyncClient(timeout=None, follow_redirects=False)
        _http_clients[key] = cli
    return cli


def _hub_headers(
    request_headers,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Build the outgoing header list: drop hop-by-hop + client bearer,
    inject hub's bearer + X-Awm-From, preserve X-Awm-As.

    ``extra_headers`` are appended last AND their keys are also stripped
    from the inbound iteration, so the hub-provided value overrides
    anything the caller sent. That's the forge-prevention seam the
    service dispatcher uses to mint X-Awm-As from the awm_as cookie.
    """
    extras = list(extra_headers or [])
    strip_extra = {k.lower() for k, _ in extras}
    out: list[tuple[str, str]] = []
    for k, v in request_headers.items():
        kl = k.lower()
        if kl in _STRIP_REQUEST or kl in strip_extra:
            continue
        out.append((k, v))
    out.append(("Authorization", f"Bearer {auth_svc.local_token()}"))
    out.append(("X-Awm-From", _local_peer_id()))
    out.extend(extras)
    return out


async def proxy_http(
    request: Request,
    target_base: str,
    extra_headers: list[tuple[str, str]] | None = None,
) -> StreamingResponse:
    """Forward ``request`` to ``target_base + request.url.path`` and
    stream the response back. Body and response are streamed; no
    materialization."""
    url = target_base.rstrip("/") + request.url.path
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = _hub_headers(request.headers, extra_headers=extra_headers)

    req = _client().build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=request.stream(),
    )
    upstream = await _client().send(req, stream=True)

    resp_headers = [
        (k, v) for k, v in upstream.headers.items()
        if k.lower() not in _STRIP_RESPONSE
    ]

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=dict(resp_headers),
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

async def proxy_ws(
    client_ws: WebSocket,
    target_base: str,
    extra_headers: list[tuple[str, str]] | None = None,
) -> None:
    """Bridge a client WS to ``target_base + path`` via websockets.connect.

    Authenticates the inbound handshake (bearer.<token> subprotocol or
    awm_session cookie), accepts the client, then opens the upstream
    connection using the HUB's own bearer + peer headers (G5). The
    client's bearer never reaches the service. ``X-Awm-As`` is forwarded
    verbatim (G6) unless overridden via ``extra_headers``.
    """
    from awm.services.auth.middleware_auth import authenticate_websocket

    # Validate inbound auth. authenticate_websocket closes on failure
    # and returns None; in that case .accept() raises and we exit.
    chosen_sub = await authenticate_websocket(client_ws)

    path = client_ws.url.path
    query = client_ws.url.query
    ws_target = _http_to_ws(target_base.rstrip("/")) + path
    if query:
        ws_target = f"{ws_target}?{query}"

    overrides = list(extra_headers or [])
    override_keys = {k.lower() for k, _ in overrides}

    outgoing_headers: list[tuple[str, str]] = [
        ("X-Awm-From", _local_peer_id()),
    ]
    x_as = client_ws.headers.get("x-awm-as")
    if x_as and "x-awm-as" not in override_keys:
        outgoing_headers.append(("X-Awm-As", x_as))
    outgoing_headers.extend(overrides)

    token = auth_svc.local_token()
    subprotocols = [f"bearer.{token}"]

    try:
        upstream = await websockets.connect(
            ws_target,
            subprotocols=subprotocols,
            additional_headers=outgoing_headers,
            max_size=None,
            open_timeout=10,
        )
    except (websockets.WebSocketException, OSError) as exc:
        log.warning("hub WS upstream connect failed (%s): %s", ws_target, exc)
        try:
            await client_ws.close(code=1011, reason="upstream connect failed")
        except Exception:
            pass
        return

    try:
        await client_ws.accept(subprotocol=chosen_sub)
    except Exception:
        try:
            await upstream.close()
        except Exception:
            pass
        return

    async def client_to_upstream():
        try:
            while True:
                msg = await client_ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    await upstream.send(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    await upstream.send(msg["text"])
        except Exception:
            return

    async def upstream_to_client():
        try:
            async for frame in upstream:
                if isinstance(frame, (bytes, bytearray)):
                    await client_ws.send_bytes(bytes(frame))
                else:
                    await client_ws.send_text(frame)
        except Exception:
            return

    c2u = asyncio.create_task(client_to_upstream())
    u2c = asyncio.create_task(upstream_to_client())
    done, pending = await asyncio.wait(
        {c2u, u2c}, return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    try:
        await upstream.close()
    except Exception:
        pass
    try:
        await client_ws.close()
    except Exception:
        pass


def _http_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


# ---------------------------------------------------------------------------
# Service RPC translator (kind="service")
# ---------------------------------------------------------------------------


async def proxy_service_http(
    request: Request,
    service_id: str,
    *,
    as_: str | None,
) -> Response:
    """Translate ``POST /svc/<name>/fn/<fn>`` into a control-WS call.

    Body is read as JSON (empty body → null args). Function is dispatched
    against the service's api manifest; declared no-response functions go
    via ``notify`` (returns 202), everything else awaits a ``reply``
    envelope (returns the result as JSON).
    """
    ch = rpc.get_control(service_id)
    if ch is None or not ch.ready.is_set():
        return JSONResponse(
            {"error": "service control channel not open"},
            status_code=503,
        )

    path = request.url.path
    # Path tail: /svc/<name>/fn/<fn>
    parts = path.split("/")
    try:
        fn_idx = parts.index("fn") + 1
        fn = parts[fn_idx]
    except (ValueError, IndexError):
        return JSONResponse({"error": "malformed function path"},
                            status_code=400)
    spec = ch.function_spec(fn) or {}
    body = await request.body()
    args: Any = None
    if body:
        try:
            args = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "request body is not valid JSON"},
                                status_code=400)
    if spec.get("no_response"):
        ch.notify(fn, args, as_=as_)
        return Response(status_code=202)
    try:
        result = await ch.call(fn, args, as_=as_,
                               timeout=float(spec.get("timeout", 30.0)))
    except rpc.RpcError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "service did not reply in time"},
                            status_code=504)
    return JSONResponse(result if result is not None else {})


async def open_session_via_http(
    request: Request,
    service_id: str,
    *,
    as_: str | None,
) -> Response:
    """Handle ``POST /svc/<name>/session/<kind>`` (the open handshake).

    Body is the ``init`` payload (json). Allocates a session_id (and, for
    direct kinds, a bridge_id), sends ``session.open`` to the service,
    awaits ``session.opened``, returns ``{ws_path: "/svc/<name>/session/<id>"}``
    on success.
    """
    ch = rpc.get_control(service_id)
    if ch is None or not ch.ready.is_set():
        return JSONResponse({"error": "service control channel not open"},
                            status_code=503)
    parts = request.url.path.split("/")
    try:
        si = parts.index("session") + 1
        kind = parts[si]
        svc_name = parts[2]
    except (ValueError, IndexError):
        return JSONResponse({"error": "malformed session path"}, status_code=400)
    spec = ch.session_spec(kind)
    if spec is None:
        return JSONResponse({"error": f"unknown session kind {kind!r}"},
                            status_code=404)
    direct = (spec.get("transport") == "direct")
    body = await request.body()
    init: Any = None
    if body:
        try:
            init = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "session init body is not valid JSON"},
                                status_code=400)
    try:
        sess = await ch.open_session(kind, init, direct=direct, as_=as_)
    except rpc.RpcError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "service did not accept session in time"},
                            status_code=504)
    return JSONResponse({
        "session_id": sess.session_id,
        "ws_path": f"/svc/{svc_name}/session/{sess.session_id}",
        "direct": direct,
    })


async def proxy_session_ws(
    client_ws: WebSocket,
    service_id: str,
    session_id: str,
) -> None:
    """Browser-side WS for an open session.

    For direct sessions: awaits the service-side bridge to open at
    ``/hub/service/bridge/{service_id}/{bridge_id}``, then byte-relays
    frames in both directions.

    For non-direct sessions: wraps each browser frame in a
    ``session.frame`` envelope onto the control WS; routes service
    ``session.frame`` envelopes back to the browser, JSON for json
    payloads or decoded base64 for binary.
    """
    from awm.services.auth.middleware_auth import authenticate_websocket

    chosen_sub = await authenticate_websocket(client_ws)

    ch = rpc.get_control(service_id)
    if ch is None or not ch.ready.is_set():
        try:
            await client_ws.close(code=1011, reason="service not connected")
        except Exception:
            pass
        return
    sess = ch.get_session(session_id)
    if sess is None:
        try:
            await client_ws.close(code=1011, reason="unknown session")
        except Exception:
            pass
        return

    try:
        await client_ws.accept(subprotocol=chosen_sub)
    except Exception:
        return

    if sess.direct:
        await _relay_direct_session(client_ws, ch, sess)
    else:
        await _relay_enveloped_session(client_ws, ch, sess)


async def _relay_direct_session(client_ws: WebSocket, ch: "rpc.ControlChannel",
                                sess: "rpc._Session") -> None:
    bridge = ch.get_bridge(sess.bridge_id) if sess.bridge_id else None
    if bridge is None:
        try:
            await client_ws.close(code=1011, reason="no bridge allocated")
        except Exception:
            pass
        return
    try:
        await asyncio.wait_for(bridge.upstream_ready.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            await client_ws.close(code=1011, reason="bridge upstream timeout")
        except Exception:
            pass
        ch.drop_session(sess.session_id)
        return
    upstream = bridge.upstream_obj
    if upstream is None:
        try:
            await client_ws.close(code=1011, reason="bridge upstream lost")
        except Exception:
            pass
        ch.drop_session(sess.session_id)
        return

    async def c2u():
        try:
            while True:
                msg = await client_ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await upstream.send_bytes(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send_text(msg["text"])
        except Exception:
            return

    async def u2c():
        try:
            while True:
                m2 = await upstream.receive()
                t2 = m2.get("type")
                if t2 == "websocket.disconnect":
                    return
                if m2.get("bytes") is not None:
                    await client_ws.send_bytes(m2["bytes"])
                elif m2.get("text") is not None:
                    await client_ws.send_text(m2["text"])
        except Exception:
            return

    tasks = [asyncio.create_task(c2u()), asyncio.create_task(u2c())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        ch.drop_session(sess.session_id)
        for closer in (client_ws.close, upstream.close):
            try:
                await closer()
            except Exception:
                pass


async def _relay_enveloped_session(client_ws: WebSocket, ch: "rpc.ControlChannel",
                                   sess: "rpc._Session") -> None:
    async def c2service():
        try:
            while True:
                msg = await client_ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    payload = {"b64": base64.b64encode(msg["bytes"]).decode("ascii")}
                elif msg.get("text") is not None:
                    try:
                        payload = {"json": json.loads(msg["text"])}
                    except json.JSONDecodeError:
                        payload = {"text": msg["text"]}
                else:
                    continue
                ch.enqueue({
                    "kind": "session.frame",
                    "session_id": sess.session_id,
                    "payload": payload,
                })
        except Exception:
            return

    async def service2c():
        try:
            while True:
                env = await sess.client_outbound.get()
                payload = env.get("payload") or {}
                if "b64" in payload:
                    try:
                        await client_ws.send_bytes(base64.b64decode(payload["b64"]))
                    except Exception:
                        pass
                elif "json" in payload:
                    await client_ws.send_text(json.dumps(payload["json"]))
                elif "text" in payload:
                    await client_ws.send_text(payload["text"])
        except Exception:
            return

    tasks = [asyncio.create_task(c2service()), asyncio.create_task(service2c())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        ch.enqueue({"kind": "session.close", "session_id": sess.session_id})
        ch.drop_session(sess.session_id)
        try:
            await client_ws.close()
        except Exception:
            pass


async def proxy_service_emit_ws(
    client_ws: WebSocket,
    service_id: str,
    topic: str,
    *,
    as_: str | None,
) -> None:
    """Browser-side WS for a non-direct emitter subscription.

    Registers a subscriber on the control channel, forwards every
    emit's payload as one JSON text frame to the browser; closes
    when the browser disconnects or the channel goes down.

    Direct emitters use a different path (a bridge) and never reach
    this handler — the route picks ``proxy_session_ws``-style relay
    for them instead.
    """
    from awm.services.auth.middleware_auth import authenticate_websocket

    chosen_sub = await authenticate_websocket(client_ws)
    ch = rpc.get_control(service_id)
    if ch is None or not ch.ready.is_set():
        try:
            await client_ws.close(code=1011, reason="service not connected")
        except Exception:
            pass
        return
    try:
        await client_ws.accept(subprotocol=chosen_sub)
    except Exception:
        return

    sub = ch.add_subscriber(topic, as_=as_)

    async def reader():
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
        except Exception:
            return

    async def writer():
        try:
            while True:
                payload = await sub.queue.get()
                try:
                    await client_ws.send_text(json.dumps(payload))
                except Exception:
                    return
        except Exception:
            return

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        ch.drop_subscriber(topic, sub.sub_id)
        try:
            await client_ws.close()
        except Exception:
            pass
