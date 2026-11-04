"""Check a grant that alcor's approver signed (fleet.md R97 item 3).

The envelope is ``{"grant": {...}, "sig": "<base64url>", "key_id": "<16 hex>"}``.
The signature is Ed25519 over the grant's canonical JSON: sorted keys,
separators ``(",", ":")``, UTF-8, integer times. The Rust check in
``awm-trust`` must produce the same bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class GrantError(PermissionError):
    """The grant is missing, forged, expired or for something else."""


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


def key_id(public_key_raw: bytes) -> str:
    return hashlib.sha256(public_key_raw).hexdigest()[:16]


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify(envelope: dict, public_key_raw: bytes, *, kind: str | None = None, node: str | None = None,
           subject: str | None = None, payload: dict | None = None,
           now: float | None = None) -> dict:
    """The grant inside ``envelope`` when it is genuine and fits, else :class:`GrantError`."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("grant"), dict):
        raise GrantError("no grant")
    grant = envelope["grant"]
    if envelope.get("key_id") != key_id(public_key_raw):
        raise GrantError("grant signed by another key")
    if any(isinstance(v, float) for v in grant.values()):
        raise GrantError("a signed time is a float, and signed times are integers")
    try:
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(
            _b64url(str(envelope.get("sig") or "")), canonical(grant))
    except (InvalidSignature, ValueError) as exc:
        raise GrantError("grant signature does not verify") from exc
    t = time.time() if now is None else now
    if not (int(grant.get("not_before", 0)) <= t < int(grant.get("not_after", 0))):
        raise GrantError("grant is outside its window")
    if kind is not None and grant.get("kind") != kind:
        raise GrantError(f"grant is for {grant.get('kind')!r}, not {kind!r}")
    if node is not None and grant.get("node") != node and node not in (grant.get("nodes") or []):
        raise GrantError(f"grant does not cover node {node!r}")
    if subject is not None and grant.get("subject") != subject:
        raise GrantError(f"grant is for {grant.get('subject')!r}, not {subject!r}")
    if payload is not None and grant.get("payload_sha256") != payload_digest(payload):
        raise GrantError("grant does not cover this request")
    return grant
