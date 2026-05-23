"""Bearer-token auth dependencies for the HTTPS listener.

All auth resolution delegates to :mod:`awm.services.auth`. Tokens come from
either the local-daemon token file or a live web-UI session cookie. WS
handshakes accept the same bearer in a ``Sec-WebSocket-Protocol`` value of
the form ``bearer.<token>`` (preferred — browsers can not set arbitrary
headers on the WS handshake), the ``awm_session`` cookie, or as a fallback
``?token=`` query string.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Query, Request, WebSocket, status

from awm.services import auth as auth_svc


def load_token() -> str | None:
    """Back-compat shim. Returns the canonical local-daemon token, or
    None when unavailable (so older code paths that compared by string
    keep working during the migration to verify_bearer)."""
    try:
        return auth_svc.local_token(generate_if_missing=False)
    except auth_svc.TokenMissing:
        return None


def _identity_from_request(request: Request) -> auth_svc.Identity | None:
    """Resolve bearer-or-cookie → identity for an incoming HTTP request."""
    auth_hdr = request.headers.get("authorization", "")
    token: str | None = None
    if auth_hdr.lower().startswith("bearer "):
        token = auth_hdr[7:].strip()
    if not token:
        cookie = request.cookies.get(auth_svc.SESSION_COOKIE)
        if cookie:
            token = cookie
    if not token:
        return None
    return auth_svc.verify_bearer(token)


def require_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> auth_svc.Identity:
    """FastAPI dependency for HTTP routes. 401 on missing/bad token.

    Returns the resolved :class:`Identity` so handlers can introspect
    the caller (e.g. peer-id, session expiry).
    """
    identity = _identity_from_request(request)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )
    return identity


async def authenticate_websocket(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> str | None:
    """Validate a WS handshake. Returns the subprotocol to echo back, or
    None when none was used. Closes with 1008 on failure.

    Order: ``Sec-WebSocket-Protocol: bearer.<token>`` → ``awm_session``
    cookie → ``?token=`` query.
    """
    chosen_sub: str | None = None
    supplied: str | None = None

    subs = websocket.headers.get("sec-websocket-protocol", "")
    for raw in [s.strip() for s in subs.split(",") if s.strip()]:
        if raw.startswith("bearer."):
            supplied = raw[len("bearer."):]
            chosen_sub = raw
            break

    if supplied is None:
        cookie = websocket.cookies.get(auth_svc.SESSION_COOKIE)
        if cookie:
            supplied = cookie

    if supplied is None and token:
        supplied = token

    if not supplied or auth_svc.verify_bearer(supplied) is None:
        await websocket.close(code=1008, reason="unauthorized")
        return None

    return chosen_sub
