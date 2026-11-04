"""The refresh step of a deploy: this node's hooks, and the route across the fleet.

A node refreshes from its own checkout and its own `deploy.toml`, never from
commands in a call, so a peer call to `routine refresh` runs only what that
node's committed config says (routines.md R25, R31, R33).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from awm.routine import deployconf, fleetconf, proc
from awm.routine.deployconf import Refresh
from awm.routine.outbound import Outbound

log = logging.getLogger("awm.routine.refresh")

MANIFEST = "awm-plugin.toml"


def default_checkout_root(workspace: Path) -> Callable[[str], Path]:
    """The live checkout of a project on this node.

    `AWM_DEPLOY_ROOT_<PROJECT>` wins. Else `projects/<p>/release`, else, for the
    project the workspace itself checks out (awm), the workspace root."""
    def root(project: str) -> Path:
        env = os.environ.get(f"AWM_DEPLOY_ROOT_{project.upper().replace('-', '_')}")
        if env:
            return Path(env)
        rel = workspace / "projects" / project / "release"
        if rel.is_dir():
            return rel
        if project == "awm" and (workspace / ".git").exists():
            return workspace
        raise FileNotFoundError(f"no live checkout of {project} on this node")
    return root


@dataclass
class NodeResult:
    node: str
    ok: bool
    detail: str = ""
    hooks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "ok": self.ok, "detail": self.detail[-2000:],
                "hooks": self.hooks}


@dataclass
class FleetResult:
    ok: bool
    nodes: list[dict[str, Any]]
    failed_node: str = ""
    error: str = ""


def _read_config(root: Path) -> Refresh:
    p = root / deployconf.FILE
    return deployconf.parse(p.read_text()).refresh if p.is_file() else Refresh()


def plugin_folders(root: Path, paths: list[str]) -> tuple[dict[str, str | None], bool]:
    """Map each changed path to its plugin folder.

    Returns ({folder: refresh command or None}, outside) where `outside` says a
    path fell outside every plugin folder."""
    folders: dict[str, str | None] = {}
    outside = False
    for rel in paths:
        parts = Path(rel).parts[:-1]
        for depth in range(len(parts), 0, -1):
            folder = Path(*parts[:depth])
            man = root / folder / MANIFEST
            if man.is_file():
                if str(folder) not in folders:
                    try:
                        plugin = tomllib.loads(man.read_text()).get("plugin", {})
                    except tomllib.TOMLDecodeError:
                        plugin = {}
                    folders[str(folder)] = plugin.get("refresh") or None
                break
        else:
            outside = True
    return folders, outside


class Refresher:
    def __init__(self, out: Outbound, *, node: str,
                 checkout_root: Callable[[str], Path],
                 fleet_config: Callable[[], fleetconf.Fleet] = fleetconf.Fleet) -> None:
        self.out = out
        self.node = node
        self.checkout_root = checkout_root
        self.fleet_config = fleet_config

    def _env(self, frm: str, to: str) -> dict[str, str]:
        return {"AWM_REFRESH_FROM": frm, "AWM_REFRESH_TO": to, "AWM_NODE_NAME": self.node}

    async def local(self, project: str, frm: str, to: str) -> NodeResult:
        """Refresh this node's checkout of ``project`` from ``frm`` to ``to``."""
        try:
            root = self.checkout_root(project)
            cfg = _read_config(root)
        except (FileNotFoundError, deployconf.ConfigError) as e:
            return NodeResult(self.node, False, str(e))
        env = self._env(frm, to)
        if cfg.sync:
            res = await proc.run(cfg.sync, cwd=str(root), timeout=cfg.timeout, env=env)
            if not res.ok:
                return NodeResult(self.node, False, f"sync {_why(res)}\n{res.output}")
            try:
                cfg = _read_config(root)
            except deployconf.ConfigError as e:
                return NodeResult(self.node, False, str(e))
        if cfg.kind == "none":
            return NodeResult(self.node, True, "no refresh configured")
        if cfg.kind == "restart":
            return await self._command(root, cfg, env, "project")
        if cfg.kind == "queue":
            return await self._queue(project, cfg, frm, to)
        return await self._hot(root, cfg, env, frm, to)

    async def _command(self, root: Path, cfg: Refresh, env: dict[str, str],
                       label: str, cwd: Path | None = None,
                       command: str | None = None) -> NodeResult:
        cmd = command or cfg.command
        if not cmd:
            return NodeResult(self.node, False, f"{label}: no refresh command")
        res = await proc.run(cmd, cwd=str(cwd or root), timeout=cfg.timeout, env=env)
        hook = {"hook": label, "code": res.code, "timed_out": res.timed_out,
                "seconds": res.seconds}
        if not res.ok:
            return NodeResult(self.node, False, f"{label} {_why(res)}\n{res.output}", [hook])
        return NodeResult(self.node, True, "", [hook])

    async def _queue(self, project: str, cfg: Refresh, frm: str, to: str) -> NodeResult:
        prompt = (f"{cfg.prompt}\n\nThe deploy of `{project}` moved release from "
                  f"{frm[:12] or '(none)'} to {to[:12]} on node {self.node}. Refresh it "
                  f"here and confirm the services run the new commit.")
        try:
            res = await self.out.start_session(
                peer="local", project=project, scope="deploy-refresh",
                prompt=prompt, started_by=f"deploy of {project} {to[:12]}",
                name=f"refresh-{project}-{to[:8]}")
        except Exception as e:  # noqa: BLE001
            return NodeResult(self.node, False, f"queue: the supervising session did not start: {e}")
        return NodeResult(self.node, True, f"supervising session {res.get('job', '')}",
                          [{"hook": "queue", "job": res.get("job", "")}])

    async def _hot(self, root: Path, cfg: Refresh, env: dict[str, str], frm: str,
                   to: str) -> NodeResult:
        try:
            changed = proc.git(root, "diff", "--name-only", frm, to).splitlines() if frm else []
        except subprocess.CalledProcessError as e:
            return NodeResult(self.node, False, f"hot: cannot diff {frm[:12]}..{to[:12]}: "
                                                f"{e.stderr.strip()[:300]}")
        if not frm:
            return await self._command(root, cfg, env, "project")
        folders, outside = plugin_folders(root, changed)
        hooks: list[dict[str, Any]] = []
        fallback = outside or any(cmd is None for cmd in folders.values())
        for folder, cmd in sorted(folders.items()):
            if cmd is None:
                continue
            r = await self._command(root, cfg, env, f"plugin {folder}", root / folder, cmd)
            hooks += r.hooks
            if not r.ok:
                r.hooks = hooks
                return r
        if fallback:
            r = await self._command(root, cfg, env, "project")
            hooks += r.hooks
            if not r.ok:
                r.hooks = hooks
                return r
        return NodeResult(self.node, True, "" if hooks else "nothing to refresh", hooks)

    async def fleet(self, project: str, frm: str, to: str, *,
                    bare: Path | None = None) -> FleetResult:
        """Refresh each node in order, stopping at the first failure (R31).

        The install config names the nodes, home base first (fleetconf). An
        empty list means this node only. A push node first takes the release
        from ``bare``."""
        fleet = self.fleet_config()
        nodes = list(fleet.rollout(project)) or [self.node]
        done: list[dict[str, Any]] = []
        for node in nodes:
            if node == self.node:
                r = await self.local(project, frm, to)
            else:
                url = fleet.push_url(node, project)
                r = await self._push(node, url, bare, to) if url else None
                if r is None:
                    r = await self._peer(node, project, frm, to)
            done.append(r.to_dict())
            if not r.ok:
                return FleetResult(False, done, node, r.detail)
        return FleetResult(True, done)

    async def _push(self, node: str, url: str, bare: Path | None,
                    to: str) -> NodeResult | None:
        """Push ``to`` into ``node``'s repo. A failure, or None to go on to its refresh."""
        if bare is None:
            return NodeResult(node, False, "push: no repo to push from")
        ref = f"refs/{self.node}/release"
        res = await proc.run(f"git -C {shlex.quote(str(bare))} push --porcelain "
                             f"{shlex.quote(url)} {to}:{ref}", cwd=str(bare), timeout=300.0)
        if not res.ok:
            return NodeResult(node, False, f"push to {url} as {ref} {_why(res)}\n{res.output}")
        return None

    async def _peer(self, node: str, project: str, frm: str, to: str) -> NodeResult:
        try:
            res = await self.out.invoke("routine", "refresh",
                                        {"project": project, "from": frm, "to": to},
                                        peer=node, timeout=1800.0)
        except Exception as e:  # noqa: BLE001
            return NodeResult(node, False, f"peer call failed: {e}")
        if not isinstance(res, dict):
            return NodeResult(node, False, f"peer returned {res!r}"[:300])
        return NodeResult(node, bool(res.get("ok")), str(res.get("detail", "")),
                          list(res.get("hooks", [])))


def _why(res: proc.Result) -> str:
    return "timed out" if res.timed_out else f"exited {res.code}"
