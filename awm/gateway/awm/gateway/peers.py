"""The peer address book — gateway runtime state for federation.

A node's gateway keeps a small directory mapping a **peer name** to that peer's
**edge URL** (its ``httpsfront`` HTTPS front, e.g. ``https://mira:12100``) and an
optional **ssh alias** (how to reach it for the ``$AWM_PEER_CRED`` fetch). This
is the *only* federation state the gateway holds: it is a **resolver**, never a
relay. A cross-peer call asks its own gateway for the peer's address, then talks
to the peer's edge **directly** — no peer bytes ever traverse this gateway.

Nothing here is synced. Each node maintains its own book; ``peer_join`` run on
both nodes makes the relationship mutual (each side records the other). Storage
is a single JSON file beside the gateway's other runtime state.
"""

from __future__ import annotations

import json
import time
from typing import Any

from awm.config import AWM_DIR

_PEERS_FILE = AWM_DIR / "state" / "peers.json"


def _normalize_edge(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url  # a bare host:port means the HTTPS edge
    return url


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_PEERS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(peers: dict[str, Any]) -> None:
    _PEERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PEERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(peers, indent=2, sort_keys=True))
    tmp.replace(_PEERS_FILE)


def add(name: str, edge_url: str, ssh_alias: str | None = None) -> dict[str, Any]:
    """Record (or update) a peer. Returns the stored entry.

    ``name`` is the peer's node name (single token, how it is addressed in
    ``<svc>@<peer>``). ``edge_url`` is the peer's HTTPS front; a bare
    ``host:port`` is coerced to ``https://host:port``. ``ssh_alias`` defaults to
    ``name`` — the ssh host the ``$AWM_PEER_CRED`` fetch targets.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("peer name is required")
    if "@" in name or "/" in name:
        raise ValueError("peer name must be a single token (no '@' or '/')")
    edge = _normalize_edge(edge_url)
    if not edge:
        raise ValueError("edge_url is required")
    peers = _load()
    entry = {
        "name": name,
        "edge_url": edge,
        "ssh_alias": (ssh_alias or "").strip() or name,
        "added_at": peers.get(name, {}).get("added_at") or time.time(),
        "updated_at": time.time(),
    }
    peers[name] = entry
    _save(peers)
    return entry


def list_all() -> list[dict[str, Any]]:
    return [_load()[k] for k in sorted(_load())]


def resolve(name: str) -> dict[str, Any] | None:
    return _load().get((name or "").strip())


def remove(name: str) -> dict[str, Any] | None:
    peers = _load()
    entry = peers.pop((name or "").strip(), None)
    if entry is not None:
        _save(peers)
    return entry
