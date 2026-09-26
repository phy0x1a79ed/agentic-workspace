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
it forwards the submitted username + password (and the client IP, for the
lockout) to ``auth.verify`` and, on success, gets back a freshly signed token
to set as the cookie. The token's ``sub`` is the verified identity the proxy
stamps on every upstream request as ``X-Awm-As: user:<sub>``.

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
# Readable twin of the session cookie: the signed-in username, for the pages'
# user chip. Carries no authority — the edge stamps identity from the session.
AS_COOKIE_NAME = "awm_as"
PEER_SUB = "peer"
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
                           bearer: str | None) -> tuple[bool, str | None, str | None]:
        """Return ``(ok, refreshed_cookie_token_or_None, sub)``.

        A valid **peer bearer** authenticates with no cookie (peers don't carry
        sessions). A valid **session cookie** authenticates and, unless the
        session has passed its hard age ceiling, yields a refreshed token to
        re-set (the sliding window).
        """
        mat = await self._material_fresh()
        if not mat:
            return False, None, None
        secret = mat["secret"]

        if bearer and bearer in set(mat.get("peer_credentials") or []):
            return True, None, PEER_SUB

        if cookie:
            claims = tokens.verify(secret, cookie)
            if claims:
                sub = str(claims.get("sub") or "operator")
                sat = int(claims.get("sat", 0))
                max_session = float(mat.get("max_session_seconds") or 0)
                if not max_session or (time.time() - sat) < max_session:
                    refreshed = tokens.mint(
                        secret, sub=sub,
                        ttl=float(mat.get("session_ttl_seconds") or 0), sat=sat)
                    return True, refreshed, sub
                # Valid but past the ceiling: allow this request, stop sliding —
                # it expires naturally at its own exp, forcing re-login.
                return True, None, sub
        return False, None, None

    async def verify_login(self, *, username: str | None, password: str,
                           client_ip: str | None) -> dict[str, Any]:
        """Forward a login to ``auth.verify``. Returns ``{ok, token, sub}`` on
        success, ``{ok: False, locked: True, retry_after}`` when the username
        or IP is locked out, else ``{ok: False}``."""
        args: dict[str, Any] = {"password": password}
        if username:
            args["username"] = username
        if client_ip:
            args["client_ip"] = client_ip
        try:
            from awm import gatewayclient
            res = await gatewayclient.call("auth", "verify", args)
        except Exception as exc:  # noqa: BLE001
            log.warning("edge: auth.verify RPC failed: %s", exc)
            return {"ok": False}
        if not isinstance(res, dict):
            return {"ok": False}
        if res.get("ok") and res.get("token"):
            return {"ok": True, "token": str(res["token"]),
                    "sub": str(res.get("sub") or "operator")}
        if res.get("locked"):
            return {"ok": False, "locked": True,
                    "retry_after": int(res.get("retry_after") or 60)}
        return {"ok": False}

    async def verify_password(self, password: str) -> str | None:
        """Shared-password login (the autologin link); a token or ``None``."""
        res = await self.verify_login(username=None, password=password, client_ip=None)
        return res.get("token") if res.get("ok") else None

    async def penpot_session(self, username: str, *,
                             stale_token: str | None = None,
                             refresh: bool = False) -> str | None:
        """The Penpot session token to hand ``username``'s browser, or ``None``.

        Penpot keeps accounts of its own, so an awm session alone does not open
        a design file; the ``auth`` service holds a Penpot credential per person
        and exchanges it for a session. The edge never sees the credential —
        only the session it becomes, which is the same split as everything else
        here: auth mints, the edge enforces.

        ``stale_token`` is the token the browser presented and the caller
        believes is dead. Passing it makes the re-login *conditional*: if
        another request has already replaced the cached token, that one comes
        back instead. Without it a burst of failing requests would each drive
        their own login.

        ``None`` means there is no Penpot identity to give — no credential
        recorded, or the ``auth`` service is unreachable. Both degrade to
        Penpot's own login screen rather than to an error page.
        """
        args: dict[str, Any] = {"username": username}
        if stale_token:
            args["stale_token"] = stale_token
        if refresh:
            args["refresh"] = True
        try:
            from awm import gatewayclient
            res = await gatewayclient.call("auth", "penpot_session", args)
        except Exception as exc:  # noqa: BLE001 — no credential, or auth is down
            log.info("edge: no Penpot session for %s: %s", username, exc)
            return None
        if isinstance(res, dict) and res.get("token"):
            return str(res["token"])
        return None

    async def session_ttl_seconds(self) -> float:
        mat = await self._material_fresh()
        return float((mat or {}).get("session_ttl_seconds") or 86400.0)
