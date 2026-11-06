"""The fleet as this node's install config states it (fleet.md R140, routines.md R31).

awm names no host. The cloud project writes `<AWM_DIR>/config.toml` per node, and
its `[fleet]` table says which node is home base and the order nodes refresh in:

    [fleet]
    home = "<node>"
    nodes = ["<home>", "<node>", ...]
    sleepers = ["<node>", ...]    # optional: nodes that may be asleep and are never woken

    [fleet.project.<name>]        # optional, for a project not landed at home
    canonical = "<node>"
    nodes = ["<canonical>", ...]

    [fleet.push.<node>]           # optional
    url = "<node>:<workspace>/projects/{project}/.bare"   # with projects, for a node
    projects = ["<name>", ...]                             # that cannot fetch from home
    artifacts = "<node>:<awm dir>/artifacts"               # for a node that builds nothing
    refresh = "<command>"                                  # with url: how home refreshes it

The node that lands a project leads its rollout. Before a node refreshes, the
landing node pushes the new release into its repo as `refs/<landing node>/release`,
fast-forward only (routines.md R31), when the node names a url. It pushes the
files the release pins (artifacts.py) when the node names an `artifacts`
directory. A node that fetches its release names only `artifacts`, and home calls
its `routine refresh`. A push node needs no routine plugin: home refreshes it by
running its `refresh` command, an ssh into the node such as deneb's (fleet.md
R79), with `AWM_REFRESH_NODE`, `AWM_REFRESH_PROJECT`, `AWM_REFRESH_FROM` and
`AWM_REFRESH_TO` set.
A sleeper that does not answer is skipped and reported, and the rollout goes on.
An absent file or table is a fleet of this node alone.
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
class Push:
    url: str
    projects: tuple[str, ...]
    artifacts: str = ""
    refresh: str = ""


@dataclass(frozen=True)
class Fleet:
    home: str = ""
    nodes: tuple[str, ...] = ()
    projects: dict[str, ProjectFleet] = field(default_factory=dict)
    push: dict[str, Push] = field(default_factory=dict)
    sleepers: tuple[str, ...] = ()

    def push_url(self, node: str, project: str) -> str:
        """Where ``node`` takes ``project``'s release by push. Empty means it fetches."""
        p = self.push.get(node)
        return p.url.format(project=project) if p and project in p.projects else ""

    def artifacts_dest(self, node: str) -> str:
        """Where ``node`` takes pinned artifacts by push. Empty means it takes none."""
        p = self.push.get(node)
        return p.artifacts if p else ""

    def refresh_command(self, node: str) -> str:
        """The command home runs to refresh push node ``node``."""
        p = self.push.get(node)
        return p.refresh if p else ""

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
    if extra := set(raw) - {"home", "nodes", "project", "push", "sleepers"}:
        raise FleetConfigError(f"{source}: unknown keys in [fleet]: {sorted(extra)}")
    home = str(raw.get("home", ""))
    nodes = _nodes(raw.get("nodes"), f"{source}: [fleet]", home)
    sleepers = _nodes(raw.get("sleepers"), f"{source}: [fleet] sleepers", "")
    if home in sleepers:
        raise FleetConfigError(f"{source}: [fleet] sleepers names home, {home!r}")
    projects: dict[str, ProjectFleet] = {}
    for name, body in (raw.get("project") or {}).items():
        where = f"{source}: [fleet.project.{name}]"
        if not isinstance(body, dict) or (extra := set(body) - {"canonical", "nodes"}):
            raise FleetConfigError(f"{where} takes only canonical and nodes")
        canonical = str(body.get("canonical", ""))
        projects[name] = ProjectFleet(canonical,
                                      _nodes(body.get("nodes"), where, canonical or home))
    push: dict[str, Push] = {}
    for node, body in (raw.get("push") or {}).items():
        where = f"{source}: [fleet.push.{node}]"
        keys = set(body) if isinstance(body, dict) else set()
        if (not isinstance(body, dict) or keys - {"url", "projects", "artifacts", "refresh"}
                or not ({"url", "projects", "refresh"} <= keys or keys == {"artifacts"}
                        or keys == {"url", "projects", "refresh", "artifacts"})):
            raise FleetConfigError(f"{where} takes url, projects and refresh, and optionally "
                                   f"artifacts, or artifacts alone")
        url, names = body.get("url", ""), body.get("projects", [])
        artifacts = body.get("artifacts", "")
        if not isinstance(artifacts, str) or ("artifacts" in keys and not artifacts):
            raise FleetConfigError(f"{where} artifacts must be a directory as <host>:<path>")
        refresh = body.get("refresh", "")
        if not isinstance(refresh, str) or ("refresh" in keys and not refresh.strip()):
            raise FleetConfigError(f"{where} refresh must be a command")
        if "url" in keys and (not isinstance(url, str) or "{project}" not in url):
            raise FleetConfigError(f"{where} url must name the repo with {{project}}")
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise FleetConfigError(f"{where} projects must be a list of project names")
        push[node] = Push(url, tuple(names), artifacts, refresh)
    return Fleet(home=home, nodes=nodes, projects=projects, push=push, sleepers=sleepers)


def load(path: Path) -> Fleet:
    return parse(path.read_text(), source=str(path)) if path.is_file() else Fleet()
