"""The peer address book — gateway runtime state for federation.

A node's gateway keeps a small directory mapping a **peer name** to that peer's
**edge URL** (its ``httpsfront`` HTTPS front, e.g. ``https://mira:12100``) and an
optional **ssh alias** (how to reach it for the ``$AWM_PEER_CRED`` fetch). This
is the *only* federation state the gateway holds: it is a **resolver**, never a
relay. A cross-peer call asks its own gateway for the peer's address, then talks
to the peer's edge **directly** — no peer bytes ever traverse this gateway.

Each entry also pins the peer's ``public_key`` with its ``key_fingerprint``.
Who may call whom is not here: the fleet edge list (``awm.config.edges``) is
the only access record. This module owns the writes; ``awm.config.peerbook`` is
the one reader, so list and resolve return exactly what services see through
``awm.config.peer_record``.

Nothing here is synced. Each node maintains its own book; ``peer_join`` run on
both nodes makes the relationship mutual (each side records the other). Storage
is a single JSON file beside the gateway's other runtime state.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Iterator

from awm.config import peerbook


def _normalize_edge(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url  # a bare host:port means the HTTPS edge
    return url


@contextmanager
def _locked_book() -> Iterator[dict[str, Any]]:
    """Hold an exclusive lock on the book and yield its parsed contents.

    Every read-modify-write runs inside this, so concurrent writers serialise.
    Raises ``ValueError`` when the book exists but cannot be parsed, so a writer
    never replaces a damaged book with a one-peer file.
    """
    path = peerbook.peers_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".json.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield peerbook.load_book(strict=True)


def _save(peers: dict[str, Any]) -> None:
    """Replace the book atomically: unique temp file, fsync, rename. Call under the lock."""
    path = peerbook.peers_file()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".peers-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(peers, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("peer name is required")
    if "@" in name or "/" in name:
        raise ValueError("peer name must be a single token (no '@' or '/')")
    return name


def _apply_key(entry: dict[str, Any], public_key: str) -> None:
    """Set ``public_key`` and its fingerprint on ``entry``; an empty key clears both."""
    public_key = (public_key or "").strip()
    if public_key:
        entry["key_fingerprint"] = peerbook._fingerprint(public_key)
        entry["public_key"] = public_key
    else:
        entry["public_key"] = None
        entry["key_fingerprint"] = None


def _stored(peers: dict[str, Any], name: str) -> dict[str, Any]:
    """The full record for ``name``, from its raw entry, or raise ``FileNotFoundError``."""
    entry = peers.get(name)
    if not isinstance(entry, dict):
        raise FileNotFoundError(f"unknown peer: {name}")
    return peerbook._normalise(name, entry)


def add(
    name: str,
    edge_url: str,
    ssh_alias: str | None = None,
    *,
    public_key: str | None = None,
) -> dict[str, Any]:
    """Record (or update) a peer. Returns the stored full record.

    ``name`` is the peer's node name (single token; the value of ``peer=`` in a
    domain call and of the ``peer`` argument to ``gatewayclient.call_peer``).
    ``edge_url`` is the peer's HTTPS front; a bare
    ``host:port`` is coerced to ``https://host:port``. ``ssh_alias`` defaults to
    ``name`` — the ssh host the ``$AWM_PEER_CRED`` fetch targets. A ``public_key``
    left out keeps the stored one.
    """
    name = _check_name(name)
    edge = _normalize_edge(edge_url)
    if not edge:
        raise ValueError("edge_url is required")
    with _locked_book() as peers:
        existing = peers.get(name) if isinstance(peers.get(name), dict) else None
        record = peerbook._normalise(name, existing or {})
        now = time.time()
        record.update(
            edge_url=edge,
            ssh_alias=(ssh_alias or "").strip() or record["ssh_alias"],
            added_at=(existing or {}).get("added_at") or now,
            updated_at=now,
        )
        if public_key is not None:
            _apply_key(record, public_key)
        peers[name] = record
        _save(peers)
        return record


def update(name: str, *, public_key: str | None = None) -> dict[str, Any]:
    """Change a recorded peer's ``public_key`` and recompute ``key_fingerprint``;
    an empty string clears both."""
    name = (name or "").strip()
    if public_key is None:
        raise ValueError("nothing to set: give public_key")
    with _locked_book() as peers:
        record = _stored(peers, name)
        _apply_key(record, public_key)
        record["updated_at"] = time.time()
        peers[name] = record
        _save(peers)
        return record


def list_all() -> list[dict[str, Any]]:
    return peerbook.list_records()


def resolve(name: str) -> dict[str, Any] | None:
    return peerbook.peer_record(name)


def remove(name: str) -> dict[str, Any] | None:
    key = (name or "").strip()
    with _locked_book() as peers:
        entry = peers.pop(key, None)
        if entry is None:
            return None
        _save(peers)
        return peerbook._normalise(key, entry) if isinstance(entry, dict) else entry
