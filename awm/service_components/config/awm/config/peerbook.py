"""Read side of the peer book (``<AWM_DIR>/state/peers.json``) and node role/swarm.

The gateway's ``peers`` module owns writes. This module is the only reader: it
loads the JSON and normalises each entry to the full record shape, so older
entries keep working.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import awm.config as _config

RELATIONS = ("domestic", "foreign")
ROLES = ("fleet", "station")
# Read categories implemented so far; the reader never validates grants against it.
GRANT_CATEGORIES = ("journals", "kb")

NODE_ROLE_ENV = "AWM_NODE_ROLE"
NODE_SWARM_ENV = "AWM_SWARM"
DEFAULT_SWARM = "tony"


def node_role() -> str:
    """This node's role, ``fleet`` or ``station`` (``AWM_NODE_ROLE``; default ``fleet``)."""
    role = (os.environ.get(NODE_ROLE_ENV) or "").strip() or "fleet"
    if role not in ROLES:
        raise ValueError(f"{NODE_ROLE_ENV}={role!r}: expected one of {ROLES}")
    return role


def node_swarm() -> str:
    """This node's swarm name (``AWM_SWARM``; default ``tony``)."""
    return (os.environ.get(NODE_SWARM_ENV) or "").strip() or DEFAULT_SWARM


def peers_file() -> Path:
    return _config.AWM_DIR / "state" / "peers.json"


def load_book() -> dict[str, Any]:
    """The raw stored book, ``{name: entry}``; empty when missing or unreadable."""
    try:
        data = json.loads(peers_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalise(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    grants = entry.get("grants")
    record = dict(entry)
    record.update(
        name=entry.get("name") or name,
        edge_url=entry.get("edge_url"),
        ssh_alias=entry.get("ssh_alias") or name,
        relation=entry.get("relation") or "domestic",
        swarm=entry.get("swarm") or node_swarm(),
        principal=entry.get("principal"),
        role=entry.get("role") or "fleet",
        public_key=entry.get("public_key"),
        key_fingerprint=entry.get("key_fingerprint"),
        grants=list(grants) if isinstance(grants, (list, tuple)) else [],
    )
    return record


def peer_record(name: str) -> dict[str, Any] | None:
    """The stored peer entry with every record field present, or ``None`` if unknown."""
    key = (name or "").strip()
    entry = load_book().get(key)
    if not isinstance(entry, dict):
        return None
    return _normalise(key, entry)


def list_records() -> list[dict[str, Any]]:
    """Every peer in the book as a full record, sorted by name."""
    book = load_book()
    return [_normalise(k, book[k]) for k in sorted(book) if isinstance(book[k], dict)]


def _fingerprint(public_key_b64: str) -> str:
    """``SHA256:`` plus the unpadded base64 of the SHA-256 of the raw key bytes.

    Defers to ``awm.config.peertoken.fingerprint`` when that module exists, so
    the book and the token verifier cannot disagree on the format.
    """
    key = (public_key_b64 or "").strip()
    try:
        raw = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"public key is not valid base64: {exc}") from exc
    if not raw:
        raise ValueError("public key is empty")
    try:
        from awm.config.peertoken import fingerprint
    except ImportError:
        digest = hashlib.sha256(raw).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    return fingerprint(key)


def peer_relation(name: str) -> str | None:
    """``domestic`` or ``foreign`` for a known peer, ``None`` for an unknown one."""
    record = peer_record(name)
    return record["relation"] if record else None


def caller_peer(as_: str | None) -> str | None:
    """The peer node name in an edge ``X-Awm-As`` value ``peer:<node>``, else ``None``.

    The name is returned as given; it is not checked against the peer book.
    """
    if not as_ or not as_.startswith("peer:"):
        return None
    return as_[len("peer:"):].strip() or None
