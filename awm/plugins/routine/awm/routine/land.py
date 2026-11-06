"""The land queue: agents bring their own branches live, one batch at a time.

`submit` queues a branch with its merge brief. One agent at a time, the merger,
holds the project's lease and runs `batch`: merge, pins, check, promote, backup,
then the rollout across the fleet. A failed step stops the batch with its state
kept, so the merger fixes it and runs `batch` again. Each run renews the lease, and
`hold` renews it while the merger works. A lease that runs out passes the batch to
the next waiting agent. `wait` gives a submitter its verdict, or names it the
merger. `bounce` returns a branch to its owner.

The skills `ci/land` and `ci/merge` carry the protocol for agents.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from awm.routine import artifacts, deployconf, fleetconf
from awm.routine.deployconf import DeployConfig

DEV, RELEASE = "dev", "release"
_ID = {"GIT_AUTHOR_NAME": "awm land", "GIT_AUTHOR_EMAIL": "land@awm.invalid",
       "GIT_COMMITTER_NAME": "awm land", "GIT_COMMITTER_EMAIL": "land@awm.invalid"}
SETTLED = ("landed", "bounced")
LEASE = 1800.0
# The last line scripts/node-refresh.sh prints.
RESULT_MARK = "AWM-REFRESH-RESULT "


def parse_result(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        if line.startswith(RESULT_MARK):
            try:
                res = json.loads(line[len(RESULT_MARK):])
            except json.JSONDecodeError:
                return None
            return res if isinstance(res, dict) and "ok" in res else None
    return None


class Stop(Exception):
    """A step failed. The batch keeps its state for the merger."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(cwd: Path, *args: str, env: dict[str, str] | None = None,
        check: bool = False, input_: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       input=input_, env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise Stop(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-800:]}")
    return r


def rev(cwd: Path, ref: str) -> str:
    r = git(cwd, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def is_ancestor(cwd: Path, a: str, b: str) -> bool:
    return git(cwd, "merge-base", "--is-ancestor", a, b).returncode == 0


class Land:
    def __init__(self, project: str, *, workspace: Path, awm_dir: Path, node: str,
                 fleet: fleetconf.Fleet) -> None:
        self.project = project
        self.workspace = workspace
        self.bare = workspace / "projects" / project / ".bare"
        if not self.bare.is_dir():
            raise SystemExit(f"no repo for {project} at {self.bare}")
        self.awm_dir = awm_dir
        self.node = node
        self.fleet = fleet
        self._restart = ""
        self.dir = awm_dir / "land" / project
        self.tickets = self.dir / "tickets"
        self.tree = self.dir / "tree"
        self.batch_file = self.dir / "batch.json"
        self.tickets.mkdir(parents=True, exist_ok=True)

    # -- state ----------------------------------------------------------------

    def ticket(self, tid: str) -> dict[str, Any]:
        p = self.tickets / f"{tid}.json"
        if not p.is_file():
            raise SystemExit(f"no ticket {tid} in {self.project}")
        return json.loads(p.read_text())

    def save(self, t: dict[str, Any]) -> None:
        _write_json(self.tickets / f"{t['id']}.json", t)

    def queued(self) -> list[dict[str, Any]]:
        ts = [json.loads(p.read_text()) for p in self.tickets.glob("*.json")]
        return sorted((t for t in ts if t["state"] == "queued"), key=lambda t: t["submitted"])

    def batch(self) -> dict[str, Any] | None:
        return json.loads(self.batch_file.read_text()) if self.batch_file.is_file() else None

    def save_batch(self, b: dict[str, Any]) -> None:
        _write_json(self.batch_file, b)

    def lease(self) -> dict[str, Any] | None:
        p = self.dir / "lease.json"
        lease = json.loads(p.read_text()) if p.is_file() else None
        return lease if lease and lease["until"] > time.time() else None

    def take_lease(self, ticket: str) -> None:
        _write_json(self.dir / "lease.json", {"ticket": ticket, "until": time.time() + LEASE})

    def drop_lease(self) -> None:
        (self.dir / "lease.json").unlink(missing_ok=True)

    def merger(self) -> str:
        """The ticket whose agent merges now, or "" when the role is free."""
        lease = self.lease()
        if lease:
            return lease["ticket"]
        return "" if self.lock_free() else "(a running batch)"

    @contextmanager
    def lock(self) -> Iterator[bool]:
        """Hold the project's land lock for the block, so two batch runs never
        overlap. Yields False when another process holds it."""
        with (self.dir / "lock").open("w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def lock_free(self) -> bool:
        with self.lock() as held:
            return held

    # -- verbs ----------------------------------------------------------------

    def submit(self, cwd: Path, brief: str, restart: str = "none") -> dict[str, Any]:
        branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD", check=True).stdout.strip()
        if branch in (DEV, RELEASE, "HEAD"):
            raise SystemExit(f"submit a feature branch, not {branch}")
        if git(cwd, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise SystemExit("commit your work first: the worktree has uncommitted changes")
        sha = git(cwd, "rev-parse", "HEAD", check=True).stdout.strip()
        if not rev(self.bare, sha):
            raise SystemExit(f"{sha[:12]} is not in {self.bare}. Push {branch} there first")
        rel = cwd.resolve().relative_to((self.workspace / "projects" / self.project).resolve())
        t = {"id": uuid.uuid4().hex[:12], "project": self.project, "scope": str(rel),
             "branch": branch, "sha": sha, "brief": brief, "state": "queued",
             "restart": restart_list(restart),
             "submitted": now(), "verdict": "", "reason": "", "batch": ""}
        self.save(t)
        return {"ticket": t["id"], "role": "queued" if self.merger() else "merger",
                "branch": branch, "sha": sha}

    def wait(self, tid: str, poll: float) -> dict[str, Any]:
        while True:
            t = self.ticket(tid)
            if t["state"] in SETTLED:
                return {"ticket": tid, "verdict": t["state"], "reason": t["reason"],
                        "release": t.get("release", "")}
            if self.merger() in ("", tid):
                return {"ticket": tid, "verdict": "merger",
                        "reason": f"no agent merges now. Load ci/merge and run "
                                  f"`land.sh batch --ticket {tid}`."}
            time.sleep(poll)

    def _own(self, ticket: str) -> None:
        if self.merger() not in ("", ticket):
            raise SystemExit(f"ticket {self.merger()} merges now. Run `land.sh wait {ticket}`")

    def hold(self, ticket: str) -> dict[str, Any]:
        self._own(ticket)
        self.take_lease(ticket)
        return {"ticket": ticket, "lease_until": now_plus(LEASE)}

    def bounce(self, tid: str, why: str, merger: str) -> dict[str, Any]:
        self._own(merger)
        with self.lock() as held:
            if not held:
                raise SystemExit("a batch is running. Bounce between batch runs")
            self.take_lease(merger)
            t = self.ticket(tid)
            t.update(state="bounced", reason=why, settled=now())
            self.save(t)
            b = self.batch()
            if b and tid in b["tickets"]:
                # The tree may hold the branch. Start the merge over without it.
                git(self.tree, "merge", "--abort")
                git(self.tree, "reset", "-q", "--hard", b["base"])
                git(self.tree, "clean", "-fdq")
                b.update(steps={}, nodes=[])
                self.save_batch(b)
        return {"ticket": tid, "verdict": "bounced", "reason": why}

    def status(self) -> dict[str, Any]:
        return {"merger": self.merger(), "lease": self.lease(), "batch": self.batch(),
                "queued": [{k: t[k] for k in ("id", "branch", "scope", "submitted")}
                           for t in self.queued()]}

    # -- the batch ------------------------------------------------------------

    def run_batch(self, ticket: str) -> int:
        if self.merger() not in ("", ticket):
            say(f"ticket {self.merger()} merges now. Run `land.sh wait {ticket}`.")
            return 3
        with self.lock() as held:
            if not held:
                say(f"a batch is running. Run `land.sh wait {ticket}`.")
                return 3
            self.take_lease(ticket)
            b = self.batch() or self._open()
            if b is None:
                say("nothing queued")
                return 0
            say(f"batch {b['id']} on release {b['base'][:12]}: " + ", ".join(
                f"{self.ticket(t)['branch']} ({t})" for t in b["tickets"]))
            try:
                self._steps(b)
            except Stop as e:
                b["failed"] = str(e)[-4000:]
                self.save_batch(b)
                self.take_lease(ticket)
                say(f"\n✗ {e}\nThe batch keeps its state, and you hold the merge lease until "
                    f"{now_plus(LEASE)}. Fix it, then run batch again. Run `land.sh hold "
                    f"--ticket {ticket}` to keep the lease while you work.")
                return 1
            self._settle(b)
            self.drop_lease()
            return 0

    def _open(self) -> dict[str, Any] | None:
        waiting = self.queued()
        if not waiting:
            return None
        base = rev(self.bare, f"refs/heads/{RELEASE}")
        if self.tree.exists():
            git(self.bare, "worktree", "remove", "--force", str(self.tree))
        git(self.bare, "worktree", "prune")
        git(self.bare, "worktree", "add", "-q", "--detach", str(self.tree), base, check=True)
        b = {"id": uuid.uuid4().hex[:12], "base": base, "started": now(),
             "tickets": [t["id"] for t in waiting], "steps": {}, "nodes": []}
        for t in waiting:
            t.update(state="batched", batch=b["id"])
            self.save(t)
        self.save_batch(b)
        return b

    def live(self, b: dict[str, Any]) -> list[dict[str, Any]]:
        return [t for t in map(self.ticket, b["tickets"]) if t["state"] != "bounced"]

    def _steps(self, b: dict[str, Any]) -> None:
        cfg = self._merge(b)
        if b.get("head") and rev(self.tree, "HEAD") != b["head"]:
            # The merger fixed the code after promote: a new release, rolled out anew.
            say(f"the batch moved past release {b['head'][:12]}: check, promote and roll "
                f"out again")
            b["steps"], b["nodes"] = {}, []
        if not b["steps"].get("pins"):
            self._pins(b, cfg)
        if not b["steps"].get("check"):
            self._check(b, cfg)
        if not b["steps"].get("promote"):
            self._promote(b, cfg)
        self._rollout(b, cfg)

    def _merge(self, b: dict[str, Any]) -> DeployConfig:
        step("merge")
        if git(self.tree, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            raise Stop(f"merge: a merge is in progress in {self.tree}. Resolve it and commit")
        for t in self.live(b):
            if is_ancestor(self.tree, t["sha"], "HEAD"):
                continue
            r = git(self.tree, "merge", "--no-edit", "-m",
                    f"land: merge {t['branch']} ({t['scope']})", t["sha"], env=_ID)
            if r.returncode != 0:
                files = git(self.tree, "diff", "--name-only", "--diff-filter=U").stdout.split()
                raise Stop(f"merge: {t['branch']} (ticket {t['id']}) conflicts with the batch in "
                           f"{', '.join(files) or r.stdout.strip()[-400:]}. The merge is left "
                           f"in progress in {self.tree}: resolve it and commit, or bounce "
                           f"the ticket")
            say(f"merged {t['branch']}")
            b["steps"].pop("pins", None)
            b["steps"].pop("check", None)
        p = self.tree / deployconf.FILE
        try:
            return deployconf.parse(p.read_text()) if p.is_file() else DeployConfig()
        except deployconf.ConfigError as e:
            raise Stop(f"merge: {e}") from e

    def _pins(self, b: dict[str, Any], cfg: DeployConfig) -> None:
        if cfg.artifact_refresh:
            step("pins")
            run(cfg.artifact_refresh, self.tree, "pins", self._log(b, "pins"),
                cfg.artifact_refresh_timeout,
                {"AWM_DIR": str(self.awm_dir), "AWM_DEPLOY_BASE": b["base"],
                 "AWM_DEPLOY_HEAD": rev(self.tree, "HEAD")})
            changed = [line[3:] for line in git(self.tree, "status", "--porcelain", "--",
                                                *cfg.artifact_pins).stdout.splitlines() if line]
            if changed:
                git(self.tree, "add", "--", *changed, check=True)
                git(self.tree, "commit", "-q", "-m", "deploy: refresh pins", env=_ID, check=True)
                say(f"committed new pins: {', '.join(changed)}")
        b["steps"]["pins"] = True
        self.save_batch(b)

    def _check(self, b: dict[str, Any], cfg: DeployConfig) -> None:
        if cfg.check:
            step("check")
            run(cfg.check, self.tree, "check", self._log(b, "check"), cfg.check_timeout,
                {"AWM_DEPLOY_BASE": b["base"], "AWM_DEPLOY_HEAD": rev(self.tree, "HEAD")})
        b["steps"]["check"] = True
        self.save_batch(b)

    def _promote(self, b: dict[str, Any], cfg: DeployConfig) -> None:
        step("promote")
        head = rev(self.tree, "HEAD")
        rel, dev = rev(self.bare, f"refs/heads/{RELEASE}"), rev(self.bare, f"refs/heads/{DEV}")
        expected = b.get("head") or b["base"]
        if rel != expected:
            raise Stop(f"promote: release moved from {expected[:12]} to {rel[:12]} under the "
                       f"batch. Merge release into {self.tree}, then run batch again")
        if dev and not is_ancestor(self.bare, dev, head):
            raise Stop(f"promote: dev at {dev[:12]} holds commits release does not. Land them "
                       f"through the queue first")
        if cfg.backup:
            self._backup_in_line(cfg.backup, head)
        clean = self._clean_checkouts()
        r = git(self.bare, "update-ref", "--stdin", input_=(
            f"start\nupdate refs/heads/{DEV} {head} {dev or '0' * 40}\n"
            f"update refs/heads/{RELEASE} {head} {rel}\ncommit\n"))
        if r.returncode != 0:
            raise Stop(f"promote: {r.stderr.strip()[-500:]}")
        for path, old in clean:
            git(path, "read-tree", "-m", "-u", old, head)
        say(f"release {rel[:12]} -> {head[:12]}")
        if cfg.backup:
            r = git(self.bare, "push", "--porcelain", cfg.backup, f"{head}:refs/heads/{RELEASE}")
            if r.returncode != 0:
                raise Stop(f"backup: push to {cfg.backup} failed: "
                           f"{(r.stderr or r.stdout).strip()[-500:]}")
            say(f"backed up to {cfg.backup}")
        b["steps"]["promote"] = True
        b["head"] = head
        self.save_batch(b)

    def _backup_in_line(self, remote: str, head: str) -> None:
        r = git(self.bare, "ls-remote", "--exit-code", remote, f"refs/heads/{RELEASE}")
        if r.returncode == 2:
            return
        if r.returncode != 0:
            raise Stop(f"backup: cannot read {RELEASE} on {remote}: {r.stderr.strip()[-300:]}")
        tip = r.stdout.split()[0]
        if not (rev(self.bare, tip) and is_ancestor(self.bare, tip, head)):
            raise Stop(f"backup: {RELEASE} on {remote} is at {tip[:12]}, which the batch does "
                       f"not contain. Bring {remote} in line by hand. Never force")

    def _clean_checkouts(self) -> list[tuple[Path, str]]:
        found, path, head = [], None, ""
        for line in git(self.bare, "worktree", "list", "--porcelain").stdout.splitlines():
            if line.startswith("worktree "):
                path = Path(line[9:])
            elif line.startswith("HEAD "):
                head = line[5:]
            elif line in (f"branch refs/heads/{DEV}", f"branch refs/heads/{RELEASE}") and path:
                if not git(path, "status", "--porcelain").stdout.strip():
                    found.append((path, head))
        return found

    # -- rollout --------------------------------------------------------------

    def _rollout(self, b: dict[str, Any], cfg: DeployConfig) -> None:
        to, frm = b["head"], b["base"]
        self._restart = batch_restart(self.live(b))
        say(f"restart: {self._restart or 'none'}")
        done = {n["node"] for n in b["nodes"] if n["ok"]}
        nodes = list(self.fleet.rollout(self.project)) or [self.node]
        pinned: list[artifacts.Artifact] | None = None
        for node in nodes:
            if node in done:
                continue
            step(f"rollout {node}")
            if node != self.node and node in self.fleet.sleepers and not _answers(node):
                say(f"{node} is asleep: skipped. It takes the release at its next refresh")
                b["nodes"].append({"node": node, "ok": True, "detail": "asleep"})
                self.save_batch(b)
                continue
            log = self._log(b, f"rollout-{node}")
            if node != self.node:
                url = self.fleet.push_url(node, self.project)
                if url:
                    run(f"git -C {q(str(self.bare))} push --porcelain {q(url)} "
                        f"{to}:refs/{self.node}/release", self.bare, f"push to {node}", log, 300)
                dest = self.fleet.artifacts_dest(node)
                if dest:
                    if pinned is None:
                        pinned = self._pinned(to)
                    for art in pinned:
                        res = asyncio.run(artifacts.push(self.awm_dir / "artifacts", art, dest))
                        if not res.ok:
                            raise Stop(f"rollout {node}: push of {art.file} to {dest} failed\n"
                                       f"{res.output[-1500:]}")
            out = run(self._refresh_command(node, cfg, frm, to), Path("/"),
                      f"rollout {node}", log, 1800, self._refresh_env(node, frm, to),
                      ok_codes=None)
            result = parse_result(out.stdout)
            ok = result["ok"] if result is not None else out.returncode == 0
            entry = {"node": node, "ok": bool(ok), "log": str(log)}
            if result is not None:
                entry["result"] = result
            b["nodes"].append(entry)
            self.save_batch(b)
            if not ok:
                where = (f"step {result.get('failed_step')}, log {result.get('log')} on {node}"
                         if result else f"exit {out.returncode}")
                raise Stop(f"rollout {node}: the refresh failed ({where}). Its output is in "
                           f"{log}. The nodes after {node} still run the old release\n"
                           f"{tail(out.stdout)}")
            say(f"{node} runs {to[:12]}")

    def _pinned(self, to: str) -> list[artifacts.Artifact]:
        try:
            arts = artifacts.pinned(self.bare, to)
            for art in arts:
                asyncio.run(artifacts.check(self.awm_dir / "artifacts", art))
        except (ValueError, deployconf.ConfigError) as e:
            raise Stop(f"rollout: artifacts: {e}") from e
        return arts

    def _refresh_command(self, node: str, cfg: DeployConfig, frm: str, to: str) -> str:
        """The node's refresh: its own command from the install config when it names
        one, else an ssh that moves its checkout and runs the project's refresh."""
        steps = []
        if cfg.refresh.sync:
            steps.append(f"AWM_REFRESH_TO={to} {cfg.refresh.sync}")
        if cfg.refresh.command:
            steps.append(f"AWM_REFRESH_RESTART={q(self._restart)} "
                         f"bash {q(cfg.refresh.command)} {to}")
        if not steps:
            return "true"
        body = " && ".join(steps)
        if node == self.node:
            root = self.workspace if self.project == "awm" else (
                self.workspace / "projects" / self.project / "release")
            return f"cd {q(str(root))} && {body}"
        if own := self.fleet.refresh_command(node):
            return own
        root = "agentic_workspace" if self.project == "awm" else \
            f"agentic_workspace/projects/{self.project}/release"
        return f"ssh -o BatchMode=yes {q(node)} {q(f'cd {root} && {body}')}"

    def _refresh_env(self, node: str, frm: str, to: str) -> dict[str, str]:
        return {"AWM_REFRESH_NODE": node, "AWM_REFRESH_PROJECT": self.project,
                "AWM_REFRESH_FROM": frm, "AWM_REFRESH_TO": to, "AWM_NODE_NAME": node,
                "AWM_REFRESH_RESTART": self._restart}

    # -- settle ---------------------------------------------------------------

    def _settle(self, b: dict[str, Any]) -> None:
        step("settle")
        for t in self.live(b):
            t.update(state="landed", release=b["head"], settled=now())
            self.save(t)
            say(f"✓ {t['branch']} ({t['id']}) landed in release {b['head'][:12]}")
        for n in b["nodes"]:
            say(f"  {n['node']}: {'ok' if n['ok'] else 'failed'}"
                + (f" ({n['detail']})" if n.get("detail") else ""))
        hist = self.dir / "batches"
        hist.mkdir(exist_ok=True)
        b["ended"] = now()
        _write_json(hist / f"{b['id']}.json", b)
        self.batch_file.unlink()
        git(self.bare, "worktree", "remove", "--force", str(self.tree))

    def _log(self, b: dict[str, Any], name: str) -> Path:
        d = self.dir / "logs" / b["id"]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}.log"


def restart_list(text: str) -> list[str]:
    """What a submitter says must restart: plugin names, `gateway`, or `none`."""
    names = [n.strip() for n in text.split(",") if n.strip()]
    if not names:
        raise SystemExit("--restart names the plugins to restart, `gateway`, or `none`")
    return [] if names == ["none"] else names


def batch_restart(tickets: list[dict[str, Any]]) -> str:
    """The union of the batch's restart lists. `gateway` restarts everything."""
    names = sorted({n for t in tickets for n in t.get("restart", [])})
    return "gateway" if "gateway" in names else ",".join(names)


def run(cmd: str, cwd: Path, label: str, log: Path, timeout: float,
        env: dict[str, str] | None = None, ok_codes: tuple[int, ...] | None = (0,),
        ) -> subprocess.CompletedProcess:
    """Run one step's command, its output to ``log``. A nonzero exit or a timeout
    stops the batch unless ``ok_codes`` is None."""
    t0 = time.monotonic()
    with log.open("a") as f:
        f.write(f"$ {cmd}\n")
        f.flush()
        try:
            p = subprocess.run(["bash", "-c", cmd], cwd=str(cwd), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            f.write(out)
            raise Stop(f"{label}: ran past its {timeout:g}s budget. Log: {log}\n{tail(out)}") from e
        f.write(p.stdout)
    say(f"  {label}: exit {p.returncode} in {time.monotonic() - t0:.0f}s (log {log})")
    if ok_codes is not None and p.returncode not in ok_codes:
        raise Stop(f"{label}: exit {p.returncode}. Log: {log}\n{tail(p.stdout)}")
    return p


def _answers(node: str) -> bool:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", node, "true"],
                          capture_output=True, timeout=20).returncode == 0


def tail(text: str, n: int = 40) -> str:
    return "\n".join(text.rstrip().splitlines()[-n:])


def q(s: str) -> str:
    return shlex.quote(s)


def now_plus(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat(
        timespec="seconds")


def say(msg: str) -> None:
    print(msg, flush=True)


def step(name: str) -> None:
    say(f"== {name} {now()}")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def _project_of(cwd: Path, workspace: Path) -> str:
    try:
        rel = cwd.resolve().relative_to((workspace / "projects").resolve())
    except ValueError:
        raise SystemExit("run this from a scope worktree under projects/, or pass --project")
    return rel.parts[0]


def main(argv: list[str] | None = None) -> int:
    from awm.config import AWM_DIR, canonical_workspace, node_name
    ap = argparse.ArgumentParser(prog="land.sh", description=__doc__.splitlines()[0])
    ap.add_argument("--project", help="Default: the project of the working directory.")
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("submit", help="Queue this worktree's branch with its merge brief.")
    s.add_argument("--brief", required=True, help="The merge brief file.")
    s.add_argument("--restart", required=True,
                   help="What must restart on each node: plugin names (a,b), gateway, or none.")
    w = sub.add_parser("wait", help="Wait for a ticket's verdict: landed, bounced or merger.")
    w.add_argument("ticket")
    w.add_argument("--poll", type=float, default=10.0)
    ba = sub.add_parser("batch", help="Run the batch as its merger.")
    ba.add_argument("--ticket", required=True, help="Your own ticket.")
    ho = sub.add_parser("hold", help="Keep the merge lease while you fix the batch.")
    ho.add_argument("--ticket", required=True, help="Your own ticket.")
    bo = sub.add_parser("bounce", help="Return a ticket to its owner.")
    bo.add_argument("ticket")
    bo.add_argument("--why", required=True)
    bo.add_argument("--ticket", dest="merger", required=True, help="Your own ticket.")
    sub.add_parser("status", help="The lock, the batch and the queue.")
    a = ap.parse_args(argv)

    ws = canonical_workspace()
    cwd = Path(os.environ.get("AWM_LAND_CWD") or os.getcwd())
    project = a.project or _project_of(cwd, ws)
    install = Path(os.environ.get("AWM_INSTALL_CONFIG") or AWM_DIR / fleetconf.FILE)
    land = Land(project, workspace=ws, awm_dir=AWM_DIR, node=node_name(),
                fleet=fleetconf.load(install))
    if a.verb == "batch":
        return land.run_batch(a.ticket)
    if a.verb == "submit":
        res = land.submit(cwd, Path(a.brief).read_text(), a.restart)
    elif a.verb == "wait":
        res = land.wait(a.ticket, a.poll)
    elif a.verb == "bounce":
        res = land.bounce(a.ticket, a.why, a.merger)
    elif a.verb == "hold":
        res = land.hold(a.ticket)
    else:
        res = land.status()
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
