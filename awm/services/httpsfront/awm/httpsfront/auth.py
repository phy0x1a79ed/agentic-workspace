"""Edge authentication for the HTTPS front.

``httpsfront`` is the single network door into AWM; the loopback gateway it
fronts stays open and auth-unaware. This module is where the door checks
credentials — it *enforces* what the ``auth`` service *mints*.

It never validates a password itself and never stores credential state. It fetches
**material** from the ``auth`` service over loopback (the signing secret + the
currently-valid peer credentials + the session-lifetime knobs), caches it for a
few seconds, and then:

* verifies + **slides** the browser session cookie **offline** with the shared
  HMAC secret (no RPC per request), and
* checks a peer's ``Authorization: Bearer`` against the valid peer credentials.

A login (``POST /__auth/login``) is the one path that calls the ``auth`` service:
it forwards the submitted password to ``auth.verify`` and, on success, gets back a
freshly signed token to set as the cookie.

Fail-closed: if the ``auth`` service is unreachable and no material is cached, the
edge authenticates nothing (login page / 401), never fails open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from awm.config import tokens

log = logging.getLogger("awm.httpsfront.auth")

COOKIE_NAME = "awm_session"
# Re-fetch edge material at most this often (peer creds rotate every ~12h, so a
# few seconds of staleness is harmless and keeps the hot path RPC-free).
_REFRESH_INTERVAL = 30.0


def bearer_of(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <t>`` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


class AuthGate:
    """Caches auth material and answers per-request authentication questions."""

    def __init__(self) -> None:
        self._material: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _material_fresh(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._material is not None and now - self._fetched_at < _REFRESH_INTERVAL:
            return self._material
        async with self._lock:
            now = time.monotonic()
            if self._material is not None and now - self._fetched_at < _REFRESH_INTERVAL:
                return self._material
            try:
                from awm import gatewayclient
                mat = await gatewayclient.call("auth", "edge_material", {})
            except Exception as exc:  # noqa: BLE001 — auth down / not yet up
                log.warning("edge: could not fetch auth material: %s", exc)
                return self._material  # keep stale if any; else None (fail closed)
            if isinstance(mat, dict) and mat.get("secret"):
                self._material = mat
                self._fetched_at = now
            return self._material

    async def authenticate(self, *, cookie: str | None,
                           bearer: str | None) -> tuple[bool, str | None]:
        """Return ``(ok, refreshed_cookie_token_or_None)``.

        A valid **peer bearer** authenticates with no cookie (peers don't carry
        sessions). A valid **session cookie** authenticates and, unless the
        session has passed its hard age ceiling, yields a refreshed token to
        re-set (the sliding window).
        """
        mat = await self._material_fresh()
        if not mat:
            return False, None
        secret = mat["secret"]

        if bearer and bearer in set(mat.get("peer_credentials") or []):
            return True, None

        if cookie:
            claims = tokens.verify(secret, cookie)
            if claims:
                sat = int(claims.get("sat", 0))
                max_session = float(mat.get("max_session_seconds") or 0)
                if not max_session or (time.time() - sat) < max_session:
                    refreshed = tokens.mint(
                        secret, sub=str(claims.get("sub", "operator")),
                        ttl=float(mat.get("session_ttl_seconds") or 0), sat=sat)
                    return True, refreshed
                # Valid but past the ceiling: allow this request, stop sliding —
                # it expires naturally at its own exp, forcing re-login.
                return True, None
        return False, None

    async def verify_password(self, password: str) -> str | None:
        """Forward a login password to ``auth.verify``; return a session token
        on success, else ``None``."""
        try:
            from awm import gatewayclient
            res = await gatewayclient.call("auth", "verify", {"password": password})
        except Exception as exc:  # noqa: BLE001
            log.warning("edge: auth.verify RPC failed: %s", exc)
            return None
        if isinstance(res, dict) and res.get("ok") and res.get("token"):
            # A successful login refreshes the material (a first-ever login right
            # after a mint should see the new peer creds too).
            return str(res["token"])
        return None

    async def session_ttl_seconds(self) -> float:
        mat = await self._material_fresh()
        return float((mat or {}).get("session_ttl_seconds") or 86400.0)
