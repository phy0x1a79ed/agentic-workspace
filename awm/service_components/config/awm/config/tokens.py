"""Self-contained HMAC session-token codec — the shared contract between the
``auth`` service (which mints tokens) and the ``httpsfront`` edge (which verifies
and slides them offline).

A token is ``<b64url(payload)>.<b64url(hmac-sha256(secret, payload_b64))>`` where
``payload`` is a small JSON object. Verification recomputes the MAC over the
payload segment and constant-time compares it, so the edge validates a cookie
**without calling the auth service on every request** — it only needs the shared
signing secret (fetched from ``auth`` once and cached, refreshed on rotation).

Both packages depend on ``awm-config``, so keeping the codec here guarantees the
minter and the verifier can never drift. Pure stdlib (hmac/hashlib/base64/json)
— no new dependency, importable from anywhere.

Claims
------
``sub``  principal the session acts as (e.g. ``"operator"``).
``sat``  *session-start* epoch seconds — fixed at login and carried unchanged
         across every sliding refresh, so a hard maximum session age can be
         enforced no matter how often the cookie is refreshed.
``exp``  expiry epoch seconds — moved forward on each refresh (the sliding
         window). A request past ``exp`` is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(secret: str, payload_b64: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                   hashlib.sha256).digest()
    return _b64u_encode(mac)


def mint(secret: str, *, sub: str = "operator", ttl: float,
         sat: int | None = None, now: float | None = None) -> str:
    """Sign a fresh session token valid for ``ttl`` seconds.

    ``sat`` (session-start) defaults to the current time on first login; a
    refresh passes the *original* ``sat`` through unchanged so the caller can cap
    total session age independently of the sliding ``exp``.
    """
    now = time.time() if now is None else now
    payload = {"sub": sub, "sat": int(sat if sat is not None else now),
               "exp": int(now + ttl)}
    payload_b64 = _b64u_encode(json.dumps(payload, separators=(",", ":"),
                                          sort_keys=True).encode("utf-8"))
    return f"{payload_b64}.{_sign(secret, payload_b64)}"


def verify(secret: str, token: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Return the token's claims if the MAC is valid and it has not expired,
    else ``None``. Never raises on a malformed token — a bad cookie is simply
    unauthenticated."""
    if not token or "." not in token:
        return None
    payload_b64, _, sig_b64 = token.partition(".")
    expected = _sign(secret, payload_b64)
    if not hmac.compare_digest(expected, sig_b64):
        return None
    try:
        claims = json.loads(_b64u_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    now = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now >= exp:
        return None
    return claims
