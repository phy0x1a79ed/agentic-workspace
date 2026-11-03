"""Check an alcor grant or order on the node that asked (fleet.md R97 item 3).

One file, needing only ``cryptography``, so any plugin can pin or vendor it. The signed
bytes are the canonical JSON of the inner object: sorted keys, separators ``,`` and ``:``,
UTF-8, integer times. Envelope: ``{"grant"|"order": {...}, "sig": b64url, "key_id": hex16}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


class GrantInvalid(Exception):
    """The envelope fails a check. The message says which."""


def canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key_id(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def load_public_key(raw_b64u: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(unb64u(raw_b64u))


def _verify(envelope: dict, field: str, pub: Ed25519PublicKey, now: float | None) -> dict:
    if not isinstance(envelope, dict) or field not in envelope:
        raise GrantInvalid(f"no {field} in the envelope")
    body = envelope[field]
    if envelope.get("key_id") != key_id(pub):
        raise GrantInvalid("the envelope names another key")
    for k, v in body.items():
        if isinstance(v, float):
            raise GrantInvalid(f"{k} is a float, and signed times are integers")
    try:
        pub.verify(unb64u(envelope.get("sig", "")), canon(body))
    except (InvalidSignature, ValueError):
        raise GrantInvalid("the signature does not verify") from None
    t = time.time() if now is None else now
    if t < body.get("not_before", 0):
        raise GrantInvalid("not valid yet")
    if t >= body["not_after"]:
        raise GrantInvalid("expired")
    return body


def verify_grant(envelope: dict, pub: Ed25519PublicKey, *, kind: str | None = None,
                 node: str | None = None, now: float | None = None) -> dict:
    """The grant body, once its signature, window and (optionally) kind and node check out."""
    body = _verify(envelope, "grant", pub, now)
    if kind is not None and body.get("kind") != kind:
        raise GrantInvalid(f"the grant is for {body.get('kind')!r}, not {kind!r}")
    if node is not None and body.get("node") != node and node not in body.get("nodes", []):
        raise GrantInvalid(f"the grant does not cover node {node!r}")
    return body


def verify_order(envelope: dict, pub: Ed25519PublicKey, *, node: str,
                 now: float | None = None) -> dict:
    """An order body for this node. The caller keeps order_id so a replay does nothing."""
    body = _verify(envelope, "order", pub, now)
    if body.get("node") != node:
        raise GrantInvalid(f"the order is for {body.get('node')!r}, not {node!r}")
    return body
