"""Device tokens: a long-lived, revocable bearer that acts as one awm user.

For an unattended device or app that must act as a user without storing a
password. A token is ``awmut1.<id>.<secret>``: ``id`` is 16 lowercase hex
characters and names the token in listings and revocations; ``secret`` is 32
random bytes, base64url without padding. The ``auth`` service keeps only the
SHA-256 of the secret, and the edge compares that digest in constant time.

The prefix lets the edge tell a device token from a node token (``awmpt1.``)
and from the legacy shared bearer before it tries to verify anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

PREFIX = "awmut1."

_ID_RE = re.compile(r"^[0-9a-f]{16}$")
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
