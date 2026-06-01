"""HTTP and WS forwarding to a registered service.

Auth model (G5/G6):

  * The hub authenticates to the service AS ITSELF — it injects
    ``Authorization: Bearer <local-auth.token>`` and
    ``X-Awm-From: <self-peer-id>`` so the service can use the existing
    ``require_peer_bearer`` dep (hub = degenerate peer).
  * The end-user's bearer (``Authorization`` header or ``awm_session``
    cookie) is STRIPPED before forwarding — the service does not see it.
  * ``X-Awm-As`` is forwarded verbatim so the service can record which
    operator the request belongs to.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from fastapi import Request, WebSocket
from starlette.responses import StreamingResponse

from awm.services import auth as auth_svc
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
    anything the caller sent. That's the forge-prevention seam stripe
    routing uses to mint X-Awm-As from the awm_as cookie.
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
    from awm.middleware_auth import authenticate_websocket

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
