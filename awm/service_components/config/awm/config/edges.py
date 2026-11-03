"""Reader of the fleet edge list, a node's ``.awm/edges.yml`` (fleet.md R87, R96, R98).

An edge ``a -> b`` lets a's user key log in to b and lets a's agents work with
b's agents as if local. The list is directional and is the only cross-node
access record: no relation, swarm or read grant exists beside it (R86).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

EDGES_FILE_ENV = "AWM_EDGES_FILE"
NETWORKS = ("lan", "tailscale", "zerotier", "internet")
ROLES = ("laptop", "desktop", "homebase", "standby", "retiring", "work",
         "station", "backup", "admin")


def edges_file() -> Path:
    """``AWM_EDGES_FILE``, else ``edges.yml`` in the node's ``.awm`` dir.

    The file is node config. The fleet's own project writes it (fleet.md R140),
    so awm ships no edge data.
    """
    env = (os.environ.get(EDGES_FILE_ENV) or "").strip()
    if env:
        return Path(env)
    import awm.config as config
    return config.AWM_DIR / "edges.yml"


def _validate(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    nodes = data.get("nodes") or {}
    edges = data.get("edges") or {}
    for name, node in nodes.items():
        role = (node or {}).get("role")
        if role not in ROLES:
            raise ValueError(f"{path}: node {name} role {role!r} not in {ROLES}")
    for src, targets in edges.items():
        if src not in nodes:
            raise ValueError(f"{path}: edge from unknown node {src}")
        for dst, net in (targets or {}).items():
            if dst not in nodes:
                raise ValueError(f"{path}: edge {src} -> unknown node {dst}")
            if dst == src:
                raise ValueError(f"{path}: self edge on {src}")
            if net not in NETWORKS:
                raise ValueError(f"{path}: edge {src} -> {dst} network {net!r} not in {NETWORKS}")
    for s in data.get("singletons") or []:
        if s.get("host") not in (data.get("singleton_doors") or {}):
            raise ValueError(f"{path}: singleton {s.get('tool')} on a host with no door")
    return data


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict[str, Any]:
    p = Path(path)
    return _validate(yaml.safe_load(p.read_text()), p)


def load() -> dict[str, Any]:
    """The parsed, validated edge list. A missing file is an empty fleet."""
    path = edges_file()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {"nodes": {}, "edges": {}, "singleton_doors": {}, "singletons": []}
    return _load(str(path), mtime)


def nodes() -> dict[str, dict[str, Any]]:
    return dict(load().get("nodes") or {})


def reaches(src: str, dst: str) -> bool:
    """Whether ``src`` holds an edge to ``dst``. A node always reaches itself."""
    if src == dst:
        return True
    return dst in ((load().get("edges") or {}).get(src) or {})


def reachable_from(src: str) -> list[str]:
    """Every node ``src`` holds an edge to, sorted."""
    return sorted(((load().get("edges") or {}).get(src) or {}).keys())


def reached_by(dst: str) -> list[str]:
    """Every node that holds an edge to ``dst``, sorted."""
    return sorted(s for s, t in (load().get("edges") or {}).items() if dst in (t or {}))


def network(src: str, dst: str) -> str | None:
    """The one network the edge ``src -> dst`` runs over (R96), or ``None`` without an edge."""
    return ((load().get("edges") or {}).get(src) or {}).get(dst)


def is_station(name: str) -> bool:
    return (nodes().get(name) or {}).get("role") == "station"


def singletons() -> list[dict[str, Any]]:
    """The singleton services, each ``{tool, host, after?}`` (R89)."""
    return list(load().get("singletons") or [])


def singleton_tools() -> list[str]:
    return [s["tool"] for s in singletons()]


def singleton_door(host: str) -> dict[str, Any] | None:
    """``{port, network}`` of ``host``'s singleton door (R98 item 2), or ``None``."""
    return (load().get("singleton_doors") or {}).get(host)


def role(name: str) -> str | None:
    """``name``'s role in the edge list (R88), or ``None`` for a node it does not list."""
    return (nodes().get(name) or {}).get("role")


def here() -> str:
    """This node's name, as the edge list names it (``awm.config.node_name``)."""
    import awm.config as config
    return config.node_name()


def admits(as_: str | None) -> bool:
    """Whether this node takes a call stamped ``as_`` (an ``X-Awm-As`` value).

    A ``peer:<node>`` stamp passes only when ``<node>`` holds an edge to this
    node; ``peer:`` naming no node fails closed. Any other stamp is not a peer
    node and passes, the bare legacy ``peer`` included.
    """
    if not (isinstance(as_, str) and as_.startswith("peer:")):
        return True
    node = as_[len("peer:"):].strip()
    return bool(node) and reaches(node, here())
