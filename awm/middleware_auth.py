"""Bearer-token auth for the exposed listener.

Token sources (first non-empty wins):
1. ``$AWM_AUTH_TOKEN`` env var
2. File at ``AUTH_TOKEN_FILE`` (default: ``$AWM_DIR/auth.token``)

File is mtime-cached and re-read on change, so rotation is a single file
rewrite — no server restart needed.

HTTP requests: ``Authorization: Bearer <token>``.
WebSocket: ``Sec-WebSocket-Protocol: bearer.<token>`` (preferred) or
``?token=<token>`` query string (documented as second-class — browsers can
not set arbitrary headers on the WS handshake).
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import Header, HTTPException, Query, Request, WebSocket, status

from awm import config


_cached_token: str | None = None
_cached_mtime: float | None = None


def load_token() -> str | None:
    """Return the current valid token, or None if none is configured.

    The env var takes priority. The file is re-read whenever its mtime
    changes, so rotation is `echo newtoken > ~/.awm/auth.token`.
    """
    global _cached_token, _cached_mtime

    env_tok = os.environ.get(config.AUTH_TOKEN_ENV)
    if env_tok:
        return env_tok.strip() or None

    path: Path = config.AUTH_TOKEN_FILE
    if not path.exists():
        return None

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _cached_token

    if _cached_token is None or _cached_mtime != mtime:
        try:
            _cached_token = path.read_text(encoding="utf-8").strip() or None
            _cached_mtime = mtime
        except OSError:
            return _cached_token

    return _cached_token


def _check(token_supplied: str | None) -> None:
    expected = load_token()
    if not expected:
        # No token configured — refuse all requests. Default-deny.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )
    if not token_supplied or not hmac.compare_digest(token_supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )


def require_bearer(
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency for HTTP routes. 401 on missing/bad token."""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    _check(token)


async def authenticate_websocket(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> str | None:
    """Validate a WebSocket handshake and return the subprotocol to accept.

    Checks subprotocols first (preferred). Looks for any value of the form
    ``bearer.<token>`` in the requested subprotocol list. Falls back to the
    ``?token=`` query string.

    Returns the subprotocol value to echo back in ``websocket.accept()`` (or
    None if none was used), so the caller can do:

        sub = await authenticate_websocket(ws)
        await ws.accept(subprotocol=sub)

    Closes the socket with 1008 (policy violation) on auth failure.
    """
    expected = load_token()
    supplied: str | None = None
    chosen_sub: str | None = None

    # Subprotocol path
    subs = websocket.headers.get("sec-websocket-protocol", "")
    for raw in [s.strip() for s in subs.split(",") if s.strip()]:
        if raw.startswith("bearer."):
            supplied = raw[len("bearer."):]
            chosen_sub = raw
            break

    # Query-string fallback
    if supplied is None and token:
        supplied = token

    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        await websocket.close(code=1008, reason="unauthorized")
        return None

    return chosen_sub
