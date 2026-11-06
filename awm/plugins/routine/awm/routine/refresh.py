"""The refresh step of a deploy: this node's hooks, and the route across the fleet.

A node refreshes from its own checkout and its own `deploy.toml`, never from
commands in a call, so a peer call to `routine refresh` runs only what that
node's committed config says (routines.md R25, R31, R33).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from awm.routine import artifacts, deployconf, fleetconf, proc
from awm.routine.deployconf import Refresh
from awm.routine.outbound import Outbound

log = logging.getLogger("awm.routine.refresh")

MANIFEST = "awm-plugin.toml"
# The last line a detached refresh prints (scripts/node-refresh.sh).
RESULT_MARK = "AWM-REFRESH-RESULT "


def parse_result(output: str) -> dict[str, Any] | None:
    """The result a detached refresh printed, or None when it printed none."""
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_MARK):
            try:
                res = json.loads(line[len(RESULT_MARK):])
            except json.JSONDecodeError:
                return None
            return res if isinstance(res, dict) and "ok" in res else None
    return None


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
    """``retry_budget`` is how long a node that gave no answer is asked again. A
    detached refresh restarts the awm that a peer call or a command reaches it through,
    so the first call often dies with it. The refresh itself runs on, and a later call
    for the same commit answers from its result."""

    def __init__(self, out: Outbound, *, node: str,
                 checkout_root: Callable[[str], Path],
                 fleet_config: Callable[[], fleetconf.Fleet] = fleetconf.Fleet,
                 artifact_store: Path | None = None,
                 awake: Callable[[str], Awaitable[bool]] | None = None,
                 retry_budget: float = 600.0, retry_every: float = 10.0) -> None:
        self.out = out
        self.awake = awake or _answers
        self.node = node
        self.checkout_root = checkout_root
        self.fleet_config = fleet_config
        self.artifact_store = artifact_store
        self.retry_budget = retry_budget
        self.retry_every = retry_every

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
        if cfg.kind == "detached":
            return await self._detached(root, cfg, env, to)
        return await self._hot(root, cfg, env, frm, to)

    async def _detached(self, root: Path, cfg: Refresh, env: dict[str, str],
                        to: str) -> NodeResult:
        """Run the node's refresh script, which runs its steps in a unit of their own
        and prints their result. A call that this awm's restart kills is made again
        for the same commit, and the script answers from the result."""
        res = await proc.run(f"bash {shlex.quote(cfg.command)} {shlex.quote(to)}",
                             cwd=str(root), timeout=cfg.timeout, env=env)
        return _from_result(self.node, res)

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
                    bare: Path | None = None, done: list[dict[str, Any]] | None = None,
                    progress: Callable[[list[dict[str, Any]]], None] | None = None,
                    ) -> FleetResult:
        """Refresh each node in order, stopping at the first failure (R31).

        ``done`` holds the nodes an earlier run of this rollout refreshed, which a
        resumed rollout skips. ``progress`` takes the list after each node.

        The install config names the nodes, home base first (fleetconf). An
        empty list means this node only. A push node takes the release from
        ``bare``, the pinned files when it names an artifacts directory, then
        home runs its refresh command. A node that fetches takes the pinned
        files when it names a directory, then refreshes itself."""
        fleet = self.fleet_config()
        nodes = list(fleet.rollout(project)) or [self.node]
        done = [n for n in done or [] if n.get("ok")]
        finished = {n["node"] for n in done}
        pinned: list[artifacts.Artifact] | None = None
        for node in nodes:
            if node in finished:
                continue
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
            if progress:
                progress(done)
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

        async def once() -> NodeResult | None:
            res = await proc.run(cmd, cwd="/", timeout=1800.0, env=env)
            if parse_result(res.output) is not None:
                return _from_result(node, res)
            answer = _json_dict(res.output)
            if answer is not None and "ok" in answer:
                return _from_dict(node, answer)
            if res.ok:
                return NodeResult(node, True, res.output[-2000:])
            raise RuntimeError(f"refresh command {_why(res)}\n{res.output}")
        return await self._until_answer(node, once)

    async def _peer(self, node: str, project: str, frm: str, to: str) -> NodeResult:
        async def once() -> NodeResult:
            try:
                res = await self.out.invoke("routine", "refresh",
                                            {"project": project, "from": frm, "to": to},
                                            peer=node, timeout=1800.0)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"peer call failed: {e}") from e
            if not isinstance(res, dict):
                return NodeResult(node, False, f"peer returned {res!r}"[:300])
            return _from_dict(node, res)
        return await self._until_answer(node, once)

    async def _until_answer(self, node: str,
                            once: Callable[[], Awaitable[NodeResult | None]]) -> NodeResult:
        """Call ``once`` until it answers. A raise is no answer, asked again every
        ``retry_every`` seconds while the retry budget lasts."""
        deadline = time.monotonic() + self.retry_budget
        while True:
            try:
                r = await once()
                if r is not None:
                    return r
                why = "no answer"
            except RuntimeError as e:
                why = str(e)
            if time.monotonic() >= deadline:
                return NodeResult(node, False, why)
            log.info("refresh of %s gave no answer, asking again: %s", node, why[:200])
            await asyncio.sleep(self.retry_every)


def _from_result(node: str, res: proc.Result) -> NodeResult:
    """A node's answer from its refresh script's output."""
    result = parse_result(res.output)
    if result is None:
        return NodeResult(node, False, f"the refresh script {_why(res)} with no result"
                                       f"\n{res.output}")
    ok = bool(result.get("ok"))
    detail = "" if ok else (f"step {result.get('failed_step') or '?'} failed; its log is "
                            f"{result.get('log', '?')} on {node}\n{res.output}")
    return NodeResult(node, ok, detail, [{"hook": "detached", **result}])


def _from_dict(node: str, res: dict[str, Any]) -> NodeResult:
    return NodeResult(node, bool(res.get("ok")), str(res.get("detail", "")),
                      list(res.get("hooks", [])))


def _json_dict(text: str) -> dict[str, Any] | None:
    try:
        res = json.loads(text)
    except json.JSONDecodeError:
        return None
    return res if isinstance(res, dict) else None


async def _answers(node: str) -> bool:
    """Whether ``node`` takes an ssh connection now. A connect never wakes a node."""
    res = await proc.run(f"ssh -o BatchMode=yes -o ConnectTimeout=8 {shlex.quote(node)} true",
                         cwd="/", timeout=20.0)
    return res.ok


def _why(res: proc.Result) -> str:
    return "timed out" if res.timed_out else f"exited {res.code}"
