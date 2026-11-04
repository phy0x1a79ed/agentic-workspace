"""Edge authentication for the HTTPS front.

``httpsfront`` is the single network door into AWM; the loopback gateway it
fronts stays open and auth-unaware. This module is where the door checks
credentials — it *enforces* what the ``auth`` service *mints*.

It never validates a password itself and never stores credential state. It fetches
**material** from the ``auth`` service over loopback (the signing secret, the
device token digests and the session-lifetime knobs), caches it for a few
seconds, and then:

* verifies + **slides** the browser session cookie **offline** with the shared
  HMAC secret (no RPC per request), and
* checks a **device token** (``awmut1.``) against the digests of the live
  device tokens, and yields the user it was minted for. Such a bearer is
  settled on its own branch: one that fails is a 401, never a fall-back to the
  cookie. A **scoped** device token also has to
  match the request's shape (:func:`awm.config.usertoken.scope_admits`), and a
  caller that passes no shape never admits one.

A login (``POST /__auth/login``) is the one path that calls the ``auth`` service:
it forwards the submitted username + password (and the client IP, for the
lockout) to ``auth.verify`` and, on success, gets back a freshly signed token
to set as the cookie. The token's ``sub`` is the verified identity the proxy
stamps on every upstream request as ``X-Awm-As: user:<sub>``.

No other node comes in here: a node reaches another over the ssh edge (fleet.md
R92), so no bearer names a node.

Fail-closed: if the ``auth`` service is unreachable and no material is cached, the
edge authenticates nothing (login page / 401), never fails open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, NamedTuple

from awm import config
from awm.config import tokens, usertoken

log = logging.getLogger("awm.httpsfront.auth")

COOKIE_NAME = "awm_session"
# Readable twin of the session cookie: the signed-in username, for the pages'
# user chip. Carries no authority — the edge stamps identity from the session.
AS_COOKIE_NAME = "awm_as"
# Re-fetch edge material at most this often; a few seconds of staleness is
# harmless and keeps the hot path RPC-free.
_REFRESH_INTERVAL = 30.0


def bearer_of(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <t>`` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def is_machine_sub(sub: str | None) -> bool:
    """The shared-password ``operator`` session, or a name that reads as a node
    (``peer``, ``peer:<node>``), as opposed to a person."""
    return sub == "operator" or sub == "peer" or bool(sub) and sub.startswith("peer:")


class Shape(NamedTuple):
    """The parts of a request a scoped device token is checked against."""

    method: str
    path: str
    raw: bytes
    query: bytes
    websocket: bool


def _device_sub(mat: dict[str, Any], token: str) -> tuple[str, str, str | None] | None:
    """``(id, user, scope)`` for a live device token, else ``None``.

    A token whose owner reads as a machine is refused: a device token speaks
    for a person, and must never carry the operator's or a peer's identity.
    """
    try:
        token_id, secret = usertoken.parse(token)
    except usertoken.TokenError:
        return None
    for entry in mat.get("device_tokens") or []:
        if entry.get("id") != token_id:
            continue
        sub = str(entry.get("sub") or "")
        if not usertoken.matches(secret, entry.get("sha256") or ""):
            return None
        if not sub or is_machine_sub(sub):
            log.warning("edge: device token %s names machine identity %r; refused",
                        token_id, sub)
            return None
        return token_id, sub, (str(entry["scope"]) if entry.get("scope") else None)
    return None


class AuthGate:
    """Caches auth material and answers per-request authentication questions."""

    def __init__(self) -> None:
        self._material: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        #: Device tokens accepted since the last fetch, ``{id: epoch_seconds}``,
        #: reported to ``auth`` on the next one.
        self._device_used: dict[str, float] = {}

    async def _material_fresh(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._material is not None and now - self._fetched_at < _REFRESH_INTERVAL:
            return self._material
        async with self._lock:
            now = time.monotonic()
            if self._material is not None and now - self._fetched_at < _REFRESH_INTERVAL:
                return self._material
            used = dict(self._device_used)
            args: dict[str, Any] = {"token_scopes": sorted(usertoken.SCOPES)}
            if used:
                args["device_used"] = used
            try:
                from awm import gatewayclient
                mat = await gatewayclient.call("auth", "edge_material", args)
            except Exception as exc:  # noqa: BLE001 — auth down / not yet up
                log.warning("edge: could not fetch auth material: %s", exc)
                return self._material  # keep stale if any; else None (fail closed)
            if isinstance(mat, dict) and mat.get("secret"):
                self._material = mat
                self._fetched_at = now
                for token_id, at in used.items():
                    if self._device_used.get(token_id) == at:
                        del self._device_used[token_id]
            return self._material

    async def authenticate(self, *, cookie: str | None, bearer: str | None,
                           shape: Shape | None = None,
                           ) -> tuple[bool, str | None, str | None]:
        """Return ``(ok, refreshed_cookie_token_or_None, sub)``.

        A **device token** authenticates as the user it was minted for, or not
        at all, and sets no cookie. A valid
        **session cookie** authenticates and, unless the session has passed its
        hard age ceiling, yields a refreshed token to re-set (the sliding
        window). A scoped device token authenticates only when ``shape`` is
        given and its scope admits it.
        """
        mat = await self._material_fresh()
        if not mat:
            return False, None, None
        secret = mat["secret"]

        if bearer and usertoken.looks_like_token(bearer):
            found = _device_sub(mat, bearer)
            if found is None:
                return False, None, None
            token_id, sub, scope = found
            if scope and (shape is None or not usertoken.scope_admits(
                    scope, method=shape.method, path=shape.path, raw=shape.raw,
                    query=shape.query, websocket=shape.websocket)):
                return False, None, None
            self._device_used[token_id] = time.time()
            return True, None, sub

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

    async def session_ttl_seconds(self) -> float:
        mat = await self._material_fresh()
        return float((mat or {}).get("session_ttl_seconds") or 86400.0)
