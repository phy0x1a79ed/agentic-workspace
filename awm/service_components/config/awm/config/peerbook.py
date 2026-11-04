"""Read side of the peer book (``<AWM_DIR>/state/peers.json``).

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

OPERATOR_DM_ACCOUNT_ENV = "AWM_OPERATOR_DM_ACCOUNT"
OPERATOR_DM_CHANNEL_ENV = "AWM_OPERATOR_DM_CHANNEL"
#: The name ``reporting`` read before the operator DM was one node setting.
LEGACY_OPERATOR_DM_CHANNEL_ENV = "AWM_REPORTING_DM_CHANNEL"
DEFAULT_OPERATOR_DM_ACCOUNT = "discord-bot"
#: Tony's DM with the bot, the channel when no setting names one.
DEFAULT_SWARM_OPERATOR_DM = "dm:188743359983124480"

#: Trust fields the book held before the edge list (fleet.md R86). A record
#: read back drops them, so the next write leaves them out of the file.
_RETIRED_FIELDS = ("relation", "swarm", "principal", "role", "grants")


def operator_dm() -> tuple[str, str]:
    """The social account and channel that reach this node's operator.

    ``AWM_OPERATOR_DM_CHANNEL`` (else ``AWM_REPORTING_DM_CHANNEL``) names the
    channel; unset, it is Tony's DM.
    """
    account = (os.environ.get(OPERATOR_DM_ACCOUNT_ENV) or "").strip() or DEFAULT_OPERATOR_DM_ACCOUNT
    channel = ((os.environ.get(OPERATOR_DM_CHANNEL_ENV) or "").strip()
               or (os.environ.get(LEGACY_OPERATOR_DM_CHANNEL_ENV) or "").strip())
    return account, channel or DEFAULT_SWARM_OPERATOR_DM


def peers_file() -> Path:
    return _config.AWM_DIR / "state" / "peers.json"


def load_book(*, strict: bool = False) -> dict[str, Any]:
    """The raw stored book, ``{name: entry}``.

    A missing file is an empty book. An unreadable or corrupt file is empty too,
    unless ``strict``: a writer must never overwrite a book it could not read,
    so ``strict`` raises ``ValueError`` instead.
    """
    path = peers_file()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if strict:
            raise ValueError(f"peer book {path} is unreadable: {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"peer book {path} is corrupt: expected a JSON object")
        return {}
    return data


def _normalise(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    record = {k: v for k, v in entry.items() if k not in _RETIRED_FIELDS}
    record.update(
        name=name,
        edge_url=entry.get("edge_url"),
        ssh_alias=entry.get("ssh_alias") or name,
        public_key=entry.get("public_key"),
        key_fingerprint=entry.get("key_fingerprint"),
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


def caller_peer(as_: str | None) -> str | None:
    """The peer node name in an edge ``X-Awm-As`` value ``peer:<node>``, else ``None``.

    The name is returned as given; it is not checked against the peer book.
    """
    if not as_ or not as_.startswith("peer:"):
        return None
    return as_[len("peer:"):].strip() or None
