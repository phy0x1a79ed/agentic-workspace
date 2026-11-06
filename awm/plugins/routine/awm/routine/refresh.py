"""The refresh step of a deploy: this node's hooks, and the route across the fleet.

A node refreshes from its own checkout and its own `deploy.toml`, never from
commands in a call, so a peer call to `routine refresh` runs only what that
node's committed config says (routines.md R25, R31, R33).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from awm.routine import artifacts, deployconf, fleetconf, proc
from awm.routine.deployconf import Refresh
from awm.routine.outbound import Outbound

log = logging.getLogger("awm.routine.refresh")

MANIFEST = "awm-plugin.toml"

# What a peer's gateway answers while its routine service restarts.
RESTARTING = ("closed its control connection", "is not up")
PEER_ATTEMPTS = 7


def default_checkout_root(workspace: Path) -> Callable[[str], Path]:
    """The live checkout of a project on this node.

    `AWM_DEPLOY_ROOT_<PROJECT>` wins. awm runs from the workspace root whenever
    that is a checkout, so for awm it comes next, ahead of the release worktree
    the promote step already moved. Else `projects/<p>/release`."""
    def root(project: str) -> Path:
        env = os.environ.get(f"AWM_DEPLOY_ROOT_{project.upper().replace('-', '_')}")
        if env:
            return Path(env)
        if project == "awm" and (workspace / ".git").exists():
            return workspace
        rel = workspace / "projects" / project / "release"
        if rel.is_dir():
            return rel
        raise FileNotFoundError(f"no live checkout of {project} on this node")
    return root


def default_holds_project(workspace: Path) -> Callable[[str], bool]:
    """Whether this node holds a bare repo of a project, to make a scope in."""
    return lambda project: (workspace / "projects" / project / ".bare").is_dir()


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
                 fleet_config: Callable[[], fleetconf.Fleet] = fleetconf.Fleet,
                 artifact_store: Path | None = None,
                 awake: Callable[[str], Awaitable[bool]] | None = None,
                 holds_project: Callable[[str], bool] = lambda _p: True) -> None:
        self.out = out
        self.holds_project = holds_project
        self.awake = awake or _answers
        self.node = node
        self.checkout_root = checkout_root
        self.fleet_config = fleet_config
        self.artifact_store = artifact_store
        self.peer_retry_wait = 10.0

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
            return await self._queue(project, root, cfg, frm, to)
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

    async def _queue(self, project: str, root: Path, cfg: Refresh, frm: str,
                     to: str) -> NodeResult:
        """Start a session that restarts the project under supervision.

        It works in the project's `deploy-refresh` scope, or in the lobby on a
        node that holds no bare repo of the project, such as one that runs awm
        from its workspace root."""
        prompt = (f"{cfg.prompt}\n\nThe deploy of `{project}` moved release from "
                  f"{frm[:12] or '(none)'} to {to[:12]} on node {self.node}. Its live "
                  f"checkout here is `{root}`: run the refresh from there. Refresh it "
                  f"here and confirm the services run the new commit.")
        name = f"refresh-{project}-{to[:8]}"
        try:
            running = await self.out.find_session(name)
        except Exception as e:  # noqa: BLE001
            log.warning("queue: cannot list this node's sessions: %s", e)
            running = ""
        if running:
            return NodeResult(self.node, True, f"supervising session {running}, already started",
                              [{"hook": "queue", "job": running}])
        has_scopes = self.holds_project(project)
        if not has_scopes:
            prompt += (f"\n\nThis node holds no `{project}` project to make a scope in, "
                       f"so you start in the lobby. Stay there and work on `{root}` by "
                       f"its path. Commit nothing in it.")
        try:
            res = await self.out.start_session(
                peer="local", project=project if has_scopes else "",
                scope="deploy-refresh" if has_scopes else "",
                prompt=prompt, started_by=f"deploy of {project} {to[:12]}",
                name=name)
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
        empty list means this node only. A push node takes the release from
        ``bare``, the pinned files when it names an artifacts directory, then
        home runs its refresh command. A node that fetches takes the pinned
        files when it names a directory, then refreshes itself."""
        fleet = self.fleet_config()
        nodes = list(fleet.rollout(project)) or [self.node]
        done: list[dict[str, Any]] = []
        pinned: list[artifacts.Artifact] | None = None
        for node in nodes:
            if node == self.node:
                r = await self.local(project, frm, to)
            elif node in fleet.sleepers and not await self.awake(node):
                r = NodeResult(node, True, "asleep: skipped and not woken. It takes this "
                                           "release at its next refresh.")
            else:
                url = fleet.push_url(node, project)
                r = await self._push(node, url, bare, to) if url else None
                dest = fleet.artifacts_dest(node)
                if r is None and dest:
                    if pinned is None:
                        pinned, r = await self._pinned(node, bare, to)
                    if r is None:
                        r = await self._push_artifacts(node, dest, pinned)
                if r is None and url:
                    r = await self._refresh_by_command(node, project, fleet.refresh_command(node),
                                                       frm, to)
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

    async def _pinned(self, node: str, bare: Path | None,
                      to: str) -> tuple[list[artifacts.Artifact], NodeResult | None]:
        """The release's pinned files, each checked against this node's store."""
        if bare is None:
            return [], NodeResult(node, False, "artifacts: no repo to read the pins from")
        try:
            pinned = await asyncio.to_thread(artifacts.pinned, bare, to)
            if pinned and self.artifact_store is None:
                raise ValueError("this node has no artifact store")
            for art in pinned:
                await artifacts.check(self.artifact_store, art)
        except (ValueError, deployconf.ConfigError) as e:
            return [], NodeResult(node, False, f"artifacts: {e}")
        return pinned, None

    async def _push_artifacts(self, node: str, dest: str,
                              pinned: list[artifacts.Artifact]) -> NodeResult | None:
        for art in pinned:
            res = await artifacts.push(self.artifact_store, art, dest)
            if not res.ok:
                return NodeResult(node, False,
                                  f"artifacts: push of {art.file} to {dest} {_why(res)}\n{res.output}")
        return None

    async def _refresh_by_command(self, node: str, project: str, cmd: str, frm: str,
                                  to: str) -> NodeResult:
        """Refresh push node ``node`` with the command its entry names, run here."""
        env = {**self._env(frm, to), "AWM_REFRESH_NODE": node, "AWM_REFRESH_PROJECT": project}
        res = await proc.run(cmd, cwd="/", timeout=1800.0, env=env)
        if not res.ok:
            return NodeResult(node, False, f"refresh command {_why(res)}\n{res.output}")
        return NodeResult(node, True, res.output[-2000:])

    async def _peer(self, node: str, project: str, frm: str, to: str) -> NodeResult:
        """Call ``node``'s refresh, again while its routine service restarts.

        The sync can move the code the peer's routine service runs from, and a
        reload then restarts that service mid-call and loses its reply. A
        refresh is idempotent, so the call runs again once the service is back."""
        for attempt in range(PEER_ATTEMPTS):
            try:
                res = await self.out.invoke("routine", "refresh",
                                            {"project": project, "from": frm, "to": to},
                                            peer=node, timeout=1800.0)
                break
            except Exception as e:  # noqa: BLE001
                if attempt + 1 == PEER_ATTEMPTS or not any(m in str(e) for m in RESTARTING):
                    return NodeResult(node, False, f"peer call failed: {e}")
                log.info("refresh on %s: %s; calling again in %.0fs", node, e,
                         self.peer_retry_wait)
                await asyncio.sleep(self.peer_retry_wait)
        if not isinstance(res, dict):
            return NodeResult(node, False, f"peer returned {res!r}"[:300])
        return NodeResult(node, bool(res.get("ok")), str(res.get("detail", "")),
                          list(res.get("hooks", [])))


async def _answers(node: str) -> bool:
    """Whether ``node`` takes an ssh connection now. A connect never wakes a node."""
    res = await proc.run(f"ssh -o BatchMode=yes -o ConnectTimeout=8 {shlex.quote(node)} true",
                         cwd="/", timeout=20.0)
    return res.ok


def _why(res: proc.Result) -> str:
    return "timed out" if res.timed_out else f"exited {res.code}"
