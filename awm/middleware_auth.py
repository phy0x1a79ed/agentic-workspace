"""Bearer-token auth dependencies for the HTTPS listener.

All auth resolution delegates to :mod:`awm.services.auth`. A bearer is
just a key — proof of possession, nothing more. Tokens come from either
the local-daemon token file, a registered peer's token file, or the
``awm_session`` HttpOnly cookie (which carries the bearer verbatim, with
no session indirection).

WS handshakes accept the same bearer in a ``Sec-WebSocket-Protocol``
value of the form ``bearer.<token>`` (preferred — browsers can not set
arbitrary headers on the WS handshake), the ``awm_session`` cookie, or
as a fallback ``?token=`` query string.

Identity (which user, which peer) is a separate claim: routes that need
it read ``X-Awm-As`` / ``X-Awm-From``. ``/peer/*`` routes use
:func:`require_peer_bearer` so the bearer is cross-checked against the
claimed peer in ``X-Awm-From``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Header, HTTPException, Query, Request, WebSocket, status

from awm.services import auth as auth_svc

log = logging.getLogger("awm.middleware_auth")


def _warn_if_legacy_home_token(presented: str | None) -> None:
    """If a presented bearer matches a stale ``~/.awm/auth.token`` (the
    pre-WORKSPACE_ROOT location) but not the active token, log a single
    WARNING so an operator notices they're using the wrong file."""
    if not presented:
        return
    legacy = Path.home() / ".awm" / "auth.token"
    try:
        if not legacy.is_file():
            return
        stale = legacy.read_text().strip()
    except OSError:
        return
    if stale and stale == presented:
        log.warning(
            "bearer matches stale ~/.awm/auth.token; the active token "
            "lives under $WORKSPACE_ROOT/.awm/auth.token — update your "
            "client config to point at the workspace path."
        )


def load_token() -> str | None:
    """Back-compat shim. Returns the canonical local-daemon token, or
    None when unavailable (so older code paths that compared by string
    keep working during the migration to verify_bearer)."""
    try:
        return auth_svc.local_token(generate_if_missing=False)
    except auth_svc.TokenMissing:
        return None


def _token_from_request(request: Request) -> str | None:
    """Extract the bearer token from an HTTP request: Authorization
    header → ``awm_session`` cookie."""
    auth_hdr = request.headers.get("authorization", "")
    if auth_hdr.lower().startswith("bearer "):
        token = auth_hdr[7:].strip()
        if token:
            return token
    cookie = request.cookies.get(auth_svc.SESSION_COOKIE)
    if cookie:
        return cookie
    return None


def require_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency for HTTP routes. 401 on missing/bad token.

    Pure-key check: any valid bearer (local or peer) is accepted.
    Returns None — handlers read identity from ``X-Awm-As`` /
    ``X-Awm-From`` if they need it, not from this dependency.
    """
    token = _token_from_request(request)
    if not auth_svc.verify_bearer(token):
        _warn_if_legacy_home_token(token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )


def require_peer_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency for ``/peer/*`` routes.

    Enforces both:

      1. ``X-Awm-From`` is present (the claimed origin peer id).
      2. The presented bearer matches the *specific* peer's token file.

    Prevents peer A from impersonating peer B by passing its own bearer
    with ``X-Awm-From: B``. Returns the verified peer id so handlers can
    use it without re-reading the header.
    """
    token = _token_from_request(request)
    claimed_peer = request.headers.get("x-awm-from", "").strip()
    if not claimed_peer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Awm-From required",
        )
    if not auth_svc.verify_peer_bearer(token, claimed_peer):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )
    request.state.from_peer = claimed_peer
    return claimed_peer


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

    if not auth_svc.verify_bearer(supplied):
        await websocket.close(code=1008, reason="unauthorized")
        return None

    return chosen_sub
