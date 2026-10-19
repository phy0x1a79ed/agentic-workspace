"""Device tokens: a long-lived, revocable bearer that acts as one awm user.

For an unattended device or app that must act as a user without storing a
password. A token is ``awmut1.<id>.<secret>``: ``id`` is 16 lowercase hex
characters and names the token in listings and revocations; ``secret`` is 32
random bytes, base64url without padding. The ``auth`` service keeps only the
SHA-256 of the secret, and the edge compares that digest in constant time.

The prefix lets the edge tell a device token from a node token (``awmpt1.``)
and from the legacy shared bearer before it tries to verify anything.

A token may carry a **scope**, which narrows it from "acts as this user
everywhere" to a few exact request shapes. ``auth`` stores the scope and the
edge enforces it with :func:`scope_admits`, so this module is the one place
both read the shapes from. A scoped token exists for a device whose bearer
crosses hosts that are not this node, such as a relay, where a leak must not
hand over the whole user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

PREFIX = "awmut1."

_ID_RE = re.compile(r"^[0-9a-f]{16}$")

#: The avatar stage session: open it, then hold its socket. The id is the
#: gateway's session id (``secrets.token_urlsafe``); the bounds leave room for a
#: longer id and still never match ``stage``.
AVATAR_STAGE = "avatar-stage"
_AVATAR_OPEN = "/svc/avatar/session/stage"
_AVATAR_SOCKET = re.compile(r"^/svc/avatar/session/[A-Za-z0-9_-]{8,64}$")

#: Every scope a token may be minted with.
SCOPES = frozenset({AVATAR_STAGE})
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class TokenError(ValueError):
    """A string that is not a well-formed device token."""


def looks_like_token(text: str | None) -> bool:
    """Whether ``text`` carries the device-token prefix, well-formed or not."""
    return bool(text) and text.startswith(PREFIX)


def mint() -> tuple[str, str, str]:
    """A fresh ``(id, secret, token)``. Only the token is shown to the holder."""
    token_id = secrets.token_hex(8)
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return token_id, secret, f"{PREFIX}{token_id}.{secret}"


def parse(token: str | None) -> tuple[str, str]:
    """``(id, secret)`` from a device token. Raises :class:`TokenError`."""
    if not looks_like_token(token):
        raise TokenError("not a device token")
    token_id, dot, secret = token[len(PREFIX):].partition(".")
    if not dot or not _ID_RE.match(token_id) or not _SECRET_RE.match(secret):
        raise TokenError("malformed device token")
    return token_id, secret


def digest(secret: str) -> str:
    """The stored form of a secret: its SHA-256, hex."""
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def matches(secret: str, stored_digest: str) -> bool:
    """Whether ``secret`` hashes to ``stored_digest``, compared in constant time."""
    return hmac.compare_digest(digest(secret).encode(), str(stored_digest or "").encode())


def scope_admits(scope: str, *, method: str, path: str, raw: bytes,
                 query: bytes, websocket: bool) -> bool:
    """Whether a token minted with ``scope`` may make this request.

    ``path`` is the decoded path and ``raw`` the path as it arrived. Both must
    name the shape, so no percent-encoding and no query string pass. An unknown
    scope admits nothing.
    """
    if query or raw != path.encode("ascii", "replace"):
        return False
    if scope == AVATAR_STAGE:
        if websocket:
            return bool(_AVATAR_SOCKET.match(path))
        return method == "POST" and path == _AVATAR_OPEN
    return False
