"""The fleet as this node's install config states it (fleet.md R140, routines.md R31).

awm names no host. The cloud project writes `<AWM_DIR>/config.toml` per node, and
its `[fleet]` table says which node is home base and the order nodes refresh in:

    [fleet]
    home = "<node>"
    nodes = ["<home>", "<node>", ...]

    [fleet.project.<name>]        # optional, for a project not landed at home
    canonical = "<node>"
    nodes = ["<canonical>", ...]

The node that lands a project leads its rollout. An absent file or table is a
fleet of this node alone.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILE = "config.toml"


class FleetConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectFleet:
    canonical: str = ""
    nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fleet:
    home: str = ""
    nodes: tuple[str, ...] = ()
    projects: dict[str, ProjectFleet] = field(default_factory=dict)

    def canonical(self, project: str) -> str:
        """The node that lands ``project``. Empty means wherever deploy is called."""
        p = self.projects.get(project)
        return (p.canonical if p and p.canonical else "") or self.home

    def rollout(self, project: str) -> tuple[str, ...]:
        """The nodes that refresh after ``project`` lands, in order."""
        p = self.projects.get(project)
        return p.nodes if p and p.nodes else self.nodes


def _nodes(raw: Any, where: str, lead: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(n, str) and n for n in raw):
        raise FleetConfigError(f"{where} nodes must be a list of node names")
    if len(set(raw)) != len(raw):
        raise FleetConfigError(f"{where} nodes lists a node twice")
    if raw and lead and raw[0] != lead:
        raise FleetConfigError(f"{where} nodes must start with {lead!r}, the node that lands")
    return tuple(raw)


def parse(text: str, *, source: str = FILE) -> Fleet:
    """Read the `[fleet]` table. Every other key of the install config is the
    gateway's, so this ignores it."""
    try:
        raw = tomllib.loads(text).get("fleet") or {}
    except tomllib.TOMLDecodeError as e:
        raise FleetConfigError(f"{source}: {e}") from e
    if extra := set(raw) - {"home", "nodes", "project"}:
        raise FleetConfigError(f"{source}: unknown keys in [fleet]: {sorted(extra)}")
    home = str(raw.get("home", ""))
    nodes = _nodes(raw.get("nodes"), f"{source}: [fleet]", home)
    projects: dict[str, ProjectFleet] = {}
    for name, body in (raw.get("project") or {}).items():
        where = f"{source}: [fleet.project.{name}]"
        if not isinstance(body, dict) or (extra := set(body) - {"canonical", "nodes"}):
            raise FleetConfigError(f"{where} takes only canonical and nodes")
        canonical = str(body.get("canonical", ""))
        projects[name] = ProjectFleet(canonical,
                                      _nodes(body.get("nodes"), where, canonical or home))
    return Fleet(home=home, nodes=nodes, projects=projects)


def load(path: Path) -> Fleet:
    return parse(path.read_text(), source=str(path)) if path.is_file() else Fleet()
