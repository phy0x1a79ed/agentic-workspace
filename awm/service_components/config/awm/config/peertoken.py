"""Node-to-node tokens: a short-lived claim that one awm node signs for another.

A token is ``awmpt1.<payload>.<signature>``. The payload is base64url(JSON) of
``{iss, aud, iat, exp}``: the node that signed it, the node it is for, and the
issue and expiry times in epoch seconds. The signature is Ed25519 over the ASCII
bytes ``awmpt1.<payload>``. The prefix lets an edge tell a node token from the
legacy shared bearer without trying to verify it.

Public keys travel as base64 of the raw 32 bytes. The private key is the base64
of its raw 32-byte seed. :func:`fingerprint` names a public key the way
``ssh-keygen`` does, so an operator can compare it out of band.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PREFIX = "awmpt1."
TOKEN_TTL_SECONDS = 300
CLOCK_SKEW_SECONDS = 60


class TokenError(ValueError):
    """A node token that must not be honoured. The message names the reason."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_key(text: str, what: str) -> bytes:
    """Raw 32 bytes from base64 in either alphabet, padded or not."""
    cleaned = (text or "").strip()
    try:
        raw = base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (binascii.Error, ValueError) as exc:
        raise TokenError(f"{what}: not base64") from exc
    if len(raw) != 32:
        raise TokenError(f"{what}: expected 32 raw bytes, got {len(raw)}")
    return raw


def _public_key(public_key_b64: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_decode_key(public_key_b64, "public key"))


def generate_private_key() -> str:
    """A fresh Ed25519 private key as base64 of its raw 32-byte seed."""
    seed = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return base64.b64encode(seed).decode("ascii")


def public_key_of(private_key_b64: str) -> str:
    """The public key for ``private_key_b64``, as base64 of the raw 32 bytes."""
    key = Ed25519PrivateKey.from_private_bytes(_decode_key(private_key_b64, "private key"))
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def fingerprint(public_key_b64: str) -> str:
    """``SHA256:`` plus the unpadded base64 of the SHA-256 of the raw public key."""
    digest = hashlib.sha256(_decode_key(public_key_b64, "public key")).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def node_label(name: str | None) -> str:
    """A node's identity in a token: the first DNS label, lowercased.

    ``node_name()`` may be an FQDN on one node and a bare name on another, so
    both ends of a token compare this form and never the raw string.
    """
    return (name or "").strip().split(".")[0].lower()


def sign(private_key_b64: str, *, iss: str, aud: str, now: float | None = None,
         ttl: float = TOKEN_TTL_SECONDS) -> str:
    """A token from node ``iss`` for node ``aud``, valid for ``ttl`` seconds."""
    iss, aud = node_label(iss), node_label(aud)
    if not iss or not aud:
        raise ValueError("iss and aud are required")
    key = Ed25519PrivateKey.from_private_bytes(_decode_key(private_key_b64, "private key"))
    issued = int(time.time() if now is None else now)
    claims = {"iss": iss, "aud": aud, "iat": issued, "exp": issued + int(ttl)}
    payload = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signature = key.sign((PREFIX + payload).encode("ascii"))
    return f"{PREFIX}{payload}.{_b64url(signature)}"


def looks_like_token(text: str | None) -> bool:
    """Whether ``text`` carries the node-token prefix, verified or not."""
    return bool(text) and text.startswith(PREFIX)


def _split(token: str) -> tuple[str, str]:
    if not looks_like_token(token):
        raise TokenError("not a node token")
    body = token[len(PREFIX):]
    payload, dot, signature = body.partition(".")
    if not dot or not payload or not signature or "." in signature:
        raise TokenError("malformed token")
    return payload, signature


def _claims_of(payload: str) -> dict[str, Any]:
    try:
        claims = json.loads(_unb64url(payload))
    except (binascii.Error, ValueError) as exc:
        raise TokenError("malformed payload") from exc
    if not isinstance(claims, dict):
        raise TokenError("malformed payload")
    return claims


def issuer_of(token: str) -> str | None:
    """The unverified ``iss`` claim, for choosing which key to verify with.

    Nothing about the token is established by this. Call :func:`verify` with
    the key of the node it names before trusting any claim.
    """
    try:
        payload, _ = _split(token)
        iss = _claims_of(payload).get("iss")
    except TokenError:
        return None
    return node_label(iss) or None if isinstance(iss, str) else None


def verify(token: str, public_key_b64: str, *, aud: str, now: float | None = None,
           skew: float = CLOCK_SKEW_SECONDS) -> dict[str, Any]:
    """The claims of ``token`` if ``public_key_b64`` signed it for ``aud``.

    Raises :class:`TokenError` on a bad signature, a different audience, an
    expired token, or one issued in the future beyond ``skew`` seconds.
    """
    payload, signature = _split(token)
    try:
        sig = _unb64url(signature)
        _public_key(public_key_b64).verify(sig, (PREFIX + payload).encode("ascii"))
    except InvalidSignature as exc:
        raise TokenError("bad signature") from exc
    except (binascii.Error, ValueError) as exc:
        raise TokenError("malformed signature") from exc
    claims = _claims_of(payload)
    iss, claimed_aud = claims.get("iss"), claims.get("aud")
    iat, exp = claims.get("iat"), claims.get("exp")
    if not isinstance(iss, str) or not node_label(iss):
        raise TokenError("missing iss")
    if not isinstance(claimed_aud, str) or node_label(claimed_aud) != node_label(aud):
        raise TokenError("wrong audience")
    # `type(...) is int` rejects bool (an int subclass) and every float, which
    # is how NaN, Infinity and 1e999 reach a JSON-parsed claim.
    if type(iat) is not int or type(exp) is not int:
        raise TokenError("iat and exp must be integers")
    current = time.time() if now is None else now
    if current > exp + skew:
        raise TokenError("expired")
    if iat > current + skew:
        raise TokenError("issued in the future")
    if exp - iat > TOKEN_TTL_SECONDS + skew:
        raise TokenError("lifetime too long")
    return claims
