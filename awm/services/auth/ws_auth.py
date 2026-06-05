"""WebSocket auth helpers for the /rooms surface.

Extends ``middleware_auth.authenticate_websocket`` with:

- ``X-Awm-As``: user identity claim. Optional cosmetic header that the
  rooms service uses to author posts as ``user:<name>`` instead of the
  default ``user:operator``. If absent, defaults to ``user:operator``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query, WebSocket

from awm.services.auth.middleware_auth import authenticate_websocket


@dataclass
class WSAuthResult:
    ok: bool
    subprotocol: str | None
    from_peer: str | None
    user_as: str
    reason: str | None = None


async def authenticate_room_ws(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> WSAuthResult:
    """Validate a WebSocket handshake for a /rooms endpoint.

    On success the caller is expected to call
    ``await websocket.accept(subprotocol=result.subprotocol)``.

    On failure the socket is already closed by the underlying helper —
    callers should check ``result.ok`` and return without further I/O.
    """
    subprotocol = await authenticate_websocket(websocket, token=token)
    # authenticate_websocket calls websocket.close(...) on failure, which
    # flips application_state to DISCONNECTED. client_state can lag.
    if (websocket.application_state.name == "DISCONNECTED"
            or websocket.client_state.name == "DISCONNECTED"):
        return WSAuthResult(False, None, None, "user:operator", "unauthorized")

    from_peer = websocket.headers.get("x-awm-from")
    user_as = websocket.headers.get("x-awm-as") or "user:operator"
    return WSAuthResult(True, subprotocol, from_peer, user_as)
