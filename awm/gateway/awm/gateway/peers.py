"""The peer address book — gateway runtime state for federation.

A node's gateway keeps a small directory mapping a **peer name** to that peer's
**edge URL** (its ``httpsfront`` HTTPS front, e.g. ``https://mira:12100``) and an
optional **ssh alias** (how to reach it for the ``$AWM_PEER_CRED`` fetch). This
is the *only* federation state the gateway holds: it is a **resolver**, never a
relay. A cross-peer call asks its own gateway for the peer's address, then talks
to the peer's edge **directly** — no peer bytes ever traverse this gateway.

Each entry also carries the peer's trust record: ``relation`` (domestic or
foreign), ``swarm``, ``principal``, ``role``, the pinned ``public_key`` with its
``key_fingerprint``, and the read ``grants`` given to a foreign peer. This module
owns the writes; ``awm.config.peerbook`` is the one reader, so list and resolve
return exactly what services see through ``awm.config.peer_record``.

Nothing here is synced. Each node maintains its own book; ``peer_join`` run on
both nodes makes the relationship mutual (each side records the other). Storage
is a single JSON file beside the gateway's other runtime state.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Iterator

from awm.config import node_role, node_swarm, peerbook

log = logging.getLogger(__name__)

#: Services that only a fleet node runs. A station that enables one is misconfigured.
FLEET_SERVICES = ("cx",)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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


def _check_final(record: dict[str, Any]) -> None:
    """Refuse a record that would pass a foreign peer off as one of this swarm."""
    if record["relation"] == "foreign" and record["swarm"] == node_swarm():
        raise ValueError(
            f"{record['name']} is foreign, so it needs an explicit swarm other than "
            f"this node's swarm ({node_swarm()!r})")


def _check_relation(value: str) -> str:
    value = (value or "").strip()
    if value not in peerbook.RELATIONS:
        raise ValueError(f"relation {value!r}: expected one of {peerbook.RELATIONS}")
    return value


def _check_role(value: str) -> str:
    value = (value or "").strip()
    if value not in peerbook.ROLES:
        raise ValueError(f"role {value!r}: expected one of {peerbook.ROLES}")
    return value


def _check_swarm(value: str) -> str:
    value = (value or "").strip()
    if not _SLUG.match(value):
        raise ValueError(f"swarm {value!r}: expected a lowercase slug")
    return value


def _check_principal(value: str) -> str:
    value = (value or "").strip()
    if not _PRINCIPAL.match(value):
        raise ValueError(f"principal {value!r}: expected a short name without spaces")
    return value


def _check_grant(value: str) -> str:
    value = (value or "").strip()
    if not _SLUG.match(value):
        raise ValueError(f"grant category {value!r}: expected a lowercase slug")
    return value


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


def _apply_fields(
    entry: dict[str, Any],
    *,
    relation: str | None,
    swarm: str | None,
    principal: str | None,
    role: str | None,
    public_key: str | None,
) -> None:
    """Validate and write the given (non-None) trust fields onto ``entry``."""
    if relation is not None:
        entry["relation"] = _check_relation(relation)
    if swarm is not None:
        entry["swarm"] = _check_swarm(swarm)
    if principal is not None:
        entry["principal"] = _check_principal(principal)
    if role is not None:
        entry["role"] = _check_role(role)
    if public_key is not None:
        _apply_key(entry, public_key)


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
    relation: str | None = None,
    swarm: str | None = None,
    principal: str | None = None,
    role: str | None = None,
    public_key: str | None = None,
) -> dict[str, Any]:
    """Record (or update) a peer. Returns the stored full record.

    ``name`` is the peer's node name (single token, how it is addressed in
    ``<svc>@<peer>``). ``edge_url`` is the peer's HTTPS front; a bare
    ``host:port`` is coerced to ``https://host:port``. ``ssh_alias`` defaults to
    ``name`` — the ssh host the ``$AWM_PEER_CRED`` fetch targets. A trust field
    left out keeps its stored value, or the domestic default for a new peer.
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
        _apply_fields(record, relation=relation, swarm=swarm, principal=principal,
                      role=role, public_key=public_key)
        _check_final(record)
        peers[name] = record
        _save(peers)
        return record


def update(
    name: str,
    *,
    relation: str | None = None,
    swarm: str | None = None,
    principal: str | None = None,
    role: str | None = None,
    public_key: str | None = None,
) -> dict[str, Any]:
    """Change trust fields on a recorded peer. Setting ``public_key`` recomputes
    ``key_fingerprint``; an empty string clears both."""
    name = (name or "").strip()
    given = (relation, swarm, principal, role, public_key)
    if all(v is None for v in given):
        raise ValueError(
            "nothing to set: give relation, swarm, principal, role or public_key")
    with _locked_book() as peers:
        record = _stored(peers, name)
        _apply_fields(record, relation=relation, swarm=swarm, principal=principal,
                      role=role, public_key=public_key)
        _check_final(record)
        record["updated_at"] = time.time()
        peers[name] = record
        _save(peers)
        return record


def grant(name: str, category: str) -> tuple[dict[str, Any], str | None]:
    """Add a read ``category`` to a peer's grants. Returns ``(record, warning)``.

    The warning is set for a domestic peer: domestic peers are trusted by
    relation, so the grant is stored but changes nothing.
    """
    name = (name or "").strip()
    category = _check_grant(category)
    with _locked_book() as peers:
        record = _stored(peers, name)
        if category not in record["grants"]:
            record["grants"].append(category)
            record["updated_at"] = time.time()
            peers[name] = record
            _save(peers)
        return record, _domestic_grant_warning(record, category)


def revoke(name: str, category: str) -> dict[str, Any]:
    """Remove a category from a peer's grants. Revoking one never granted is a no-op."""
    name = (name or "").strip()
    category = _check_grant(category)
    with _locked_book() as peers:
        record = _stored(peers, name)
        if category in record["grants"]:
            record["grants"].remove(category)
            record["updated_at"] = time.time()
            peers[name] = record
            _save(peers)
        return record


def _domestic_grant_warning(record: dict[str, Any], category: str) -> str | None:
    if record["relation"] != "domestic":
        return None
    return (f"{record['name']} is a domestic peer; grant {category!r} is stored "
            "but has no effect (grants only limit foreign peers)")


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


def warn_station_fleet_services() -> list[str]:
    """Log a warning when this node is a station with a fleet service enabled.

    Returns the offending service names (empty when the node is fine).
    """
    try:
        if node_role() != "station":
            return []
        from awm.gateway.hub import discovery

        enabled = [s.name for s in discovery.discover_services()
                   if s.enabled and s.name in FLEET_SERVICES]
    except Exception as exc:  # noqa: BLE001 - a startup check must never block boot
        log.warning("station/fleet service check skipped: %s", exc)
        return []
    if enabled:
        log.warning(
            "this node is a station (AWM_NODE_ROLE=station) but fleet service(s) "
            "%s are enabled; stations should not run them", ", ".join(enabled))
    return enabled
