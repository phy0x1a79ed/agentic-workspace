"""The deploy engine: tickets, batches and the six steps (routines.md R19 to R31).

A `deploy` call pins a scope's branch head as a ticket and returns at once. One
worker per project takes every waiting ticket into a batch and runs pin, merge,
check, promote, backup, refresh and report. Only this engine writes `dev` and
`release` (R29), so a ref that moved under a batch is a failure, never a
force. The backup remote takes `release` the same way: a backup that diverged
stops the batch before release moves (install.md R114).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awm.routine import deployconf, proc
from awm.routine.deployconf import ConfigError, DeployConfig
from awm.routine.escalate import escalate
from awm.routine.outbound import Outbound
from awm.routine.refresh import Refresher
from awm.routine.store import LiveRunExists, Store

log = logging.getLogger("awm.routine.deploy")

DEV, RELEASE = "dev", "release"
_ID = {"GIT_AUTHOR_NAME": "awm deploy", "GIT_AUTHOR_EMAIL": "deploy@awm.invalid",
       "GIT_COMMITTER_NAME": "awm deploy", "GIT_COMMITTER_EMAIL": "deploy@awm.invalid"}


class StepFailed(Exception):
    def __init__(self, step: str, error: str, *, landed: bool = False) -> None:
        super().__init__(f"{step}: {error}")
        self.step = step
        self.error = error
        self.landed = landed


def _git(cwd: Path | str, *args: str, input: str | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          input=input, env={**os.environ, **(env or {})})


def _rev(bare: Path, ref: str) -> str:
    r = _git(bare, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _is_ancestor(bare: Path, a: str, b: str) -> bool:
    return _git(bare, "merge-base", "--is-ancestor", a, b).returncode == 0


@dataclass
class Submission:
    project: str
    scope: str
    branch: str
    sha: str
    card: str = ""


class Deployer:
    def __init__(self, store: Store, out: Outbound, *, workspace: Path,
                 refresher: Refresher) -> None:
        self.store = store
        self.out = out
        self.workspace = Path(workspace)
        self.refresher = refresher
        self._wake = asyncio.Event()
        self._live: dict[str, asyncio.Task] = {}

    # -- submit -------------------------------------------------------------

    def bare(self, project: str) -> Path:
        p = self.workspace / "projects" / project / ".bare"
        if not p.is_dir():
            raise FileNotFoundError(f"no repo for project {project} on this node ({p})")
        return p

    def resolve(self, *, project: str = "", scope: str = "", branch: str = "",
                sha: str = "", cwd: str = "") -> Submission:
        """What a deploy call submits: the caller's scope, its branch and its head."""
        if not (project and scope) and cwd:
            from awm.config import scope_of
            p, s = scope_of(cwd, self.workspace / "projects")
            project, scope = project or (p or ""), scope or (s or "")
        if not (project and scope):
            raise ValueError("cannot tell the calling scope. Call from a scope worktree, "
                             "or pass project and scope")
        bare = self.bare(project)
        worktree = self.workspace / "projects" / project / scope
        if not branch:
            if (worktree / ".git").exists():
                branch = proc.git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
            else:
                branch = f"feat/{scope}"
        if branch in (DEV, RELEASE, "HEAD"):
            raise ValueError(f"deploy lands a feature branch, not {branch}")
        head = _rev(bare, f"refs/heads/{branch}")
        if sha:
            if not _rev(bare, sha):
                raise ValueError(f"{sha[:12]} is not in the {project} repo on this node. "
                                 f"Push {branch} first")
            sha = _rev(bare, sha)
        elif head:
            sha = head
        else:
            raise ValueError(f"no branch {branch} in the {project} repo")
        return Submission(project, scope, branch, sha)

    def submit(self, sub: Submission, caller: str = "") -> dict[str, Any]:
        ticket = self.store.add_ticket(project=sub.project, scope=sub.scope,
                                       branch=sub.branch, sha=sub.sha, caller=caller,
                                       card=sub.card)
        self._wake.set()
        return ticket

    # -- worker -------------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            self._start_waiting()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 30.0)
            except asyncio.TimeoutError:
                pass

    def _start_waiting(self) -> None:
        for project in self.store.projects_with_queue():
            if project in self._live:
                continue
            task = asyncio.create_task(self._drain_project(project))
            self._live[project] = task
            task.add_done_callback(lambda _t, p=project: self._done(p))

    def _done(self, project: str) -> None:
        self._live.pop(project, None)
        self._wake.set()

    async def drain(self) -> None:
        """Run batches until no ticket waits. For tests and the dry run."""
        while True:
            self._start_waiting()
            if not self._live:
                return
            await asyncio.gather(*list(self._live.values()), return_exceptions=True)

    async def _drain_project(self, project: str) -> None:
        while True:
            try:
                opened = self.store.open_batch(project)
            except LiveRunExists:
                return
            if opened is None:
                return
            batch, tickets = opened
            try:
                await self.run_batch(batch, tickets)
            except Exception as e:  # noqa: BLE001 — a crashed batch is a failed batch
                log.exception("batch %s crashed", batch["id"])
                await self._fail(batch, tickets, "(service)", repr(e))

    # -- the six steps ------------------------------------------------------

    async def run_batch(self, batch: dict[str, Any], tickets: list[dict[str, Any]]) -> None:
        bid, project = batch["id"], batch["project"]
        bare = self.bare(project)
        tmp = Path(tempfile.mkdtemp(prefix=f"awm-deploy-{project}-"))
        wt = tmp / "tree"
        state: dict[str, str] = {}
        cfg: DeployConfig | None = None
        try:
            self._pin(bid, bare, tickets, state)
            await asyncio.to_thread(self._merge, bid, bare, wt, tickets, state)
            cfg = self._config(wt)
            await self._check(bid, wt, cfg, state)
            await asyncio.to_thread(self._promote, bid, bare, state, cfg)
            await asyncio.to_thread(self._backup, bid, bare, state, cfg)
        except StepFailed as f:
            await self._fail(batch, tickets, f.step, f.error, cfg=cfg, landed=f.landed)
            return
        finally:
            _git(bare, "worktree", "remove", "--force", str(wt))
            _git(bare, "worktree", "prune")
            shutil.rmtree(tmp, ignore_errors=True)
        fleet = await self.refresher.fleet(project, state["release_before"], state["head"],
                                           bare=bare)
        self.store.update_batch(bid, nodes=fleet.nodes)
        self.store.record_batch_step(bid, {"step": "refresh", "ok": fleet.ok,
                                           "failed_node": fleet.failed_node})
        if not fleet.ok:
            await self._fail(batch, tickets, "refresh",
                             f"node {fleet.failed_node}: {fleet.error}", cfg=cfg,
                             landed=True)
            return
        mark = f"✓ landed in release {state['head'][:12]}"
        self.store.finish_batch(bid, "landed", mark=mark)
        await self._report(project, tickets, bid, mark)

    def _pin(self, bid: str, bare: Path, tickets: list[dict[str, Any]],
             state: dict[str, str]) -> None:
        missing = [t for t in tickets if not _rev(bare, t["sha"])]
        state["dev_before"] = _rev(bare, f"refs/heads/{DEV}")
        state["release_before"] = _rev(bare, f"refs/heads/{RELEASE}")
        self.store.update_batch(bid, dev_before=state["dev_before"],
                                release_before=state["release_before"])
        self.store.record_batch_step(bid, {"step": "pin", "ok": not missing,
                                           "tickets": [t["id"] for t in tickets]})
        if missing:
            raise StepFailed("pin", "commits not in the repo: " + ", ".join(
                f"{t['branch']}@{t['sha'][:12]}" for t in missing))
        if not state["dev_before"]:
            raise StepFailed("pin", f"the repo has no {DEV} branch")

    def _merge(self, bid: str, bare: Path, wt: Path, tickets: list[dict[str, Any]],
               state: dict[str, str]) -> None:
        r = _git(bare, "worktree", "add", "--detach", str(wt), state["dev_before"])
        if r.returncode != 0:
            raise StepFailed("merge", f"temporary worktree: {r.stderr.strip()}")
        merged: list[str] = []
        sources = [(f"{RELEASE} (ahead of {DEV})", state["release_before"])] if (
            state["release_before"]
            and not _is_ancestor(bare, state["release_before"], state["dev_before"])) else []
        sources += [(f"{t['branch']} ({t['scope']})", t["sha"]) for t in tickets]
        for label, sha in sources:
            r = _git(wt, "merge", "--no-edit", "-m", f"deploy: merge {label}", sha, env=_ID)
            if r.returncode != 0:
                conflicts = _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
                _git(wt, "merge", "--abort")
                self.store.record_batch_step(bid, {"step": "merge", "ok": False,
                                                   "merged": merged, "conflict": label})
                raise StepFailed("merge", f"{label} conflicts with {', '.join(merged) or DEV}"
                                 f" in: {', '.join(conflicts) or r.stdout.strip()[-500:]}")
            merged.append(label)
        state["head"] = proc.git(wt, "rev-parse", "HEAD")
        self.store.record_batch_step(bid, {"step": "merge", "ok": True, "merged": merged,
                                           "head": state["head"]})

    def _config(self, wt: Path) -> DeployConfig:
        p = wt / deployconf.FILE
        try:
            return deployconf.parse(p.read_text()) if p.is_file() else DeployConfig()
        except ConfigError as e:
            raise StepFailed("check", str(e)) from e

    async def _check(self, bid: str, wt: Path, cfg: DeployConfig,
                     state: dict[str, str]) -> None:
        if not cfg.check:
            self.store.record_batch_step(bid, {"step": "check", "ok": True,
                                               "note": "no check configured"})
            return
        res = await proc.run(cfg.check, cwd=str(wt), timeout=cfg.check_timeout,
                             env={"AWM_DEPLOY_BASE": state["dev_before"],
                                  "AWM_DEPLOY_HEAD": state["head"]})
        self.store.record_batch_step(bid, {"step": "check", "ok": res.ok, "code": res.code,
                                           "timed_out": res.timed_out,
                                           "seconds": res.seconds})
        if res.timed_out:
            raise StepFailed("check timeout",
                             f"the check ran past its {cfg.check_timeout:g}s budget\n"
                             f"{res.output}")
        if not res.ok:
            raise StepFailed("check", f"exited {res.code}\n{res.output}")

    def _promote(self, bid: str, bare: Path, state: dict[str, str],
                 cfg: DeployConfig) -> None:
        head, dev0, rel0 = state["head"], state["dev_before"], state["release_before"]
        if cfg.backup:
            self._backup_in_line(bid, bare, cfg.backup, head)
        clean = self._clean_checkouts(bare, (DEV, RELEASE))
        # One transaction moves both refs, each only from the value pin read.
        rel_old = rel0 or "0" * 40
        r = _git(bare, "update-ref", "--stdin", input=(
            f"start\nupdate refs/heads/{DEV} {head} {dev0}\n"
            f"update refs/heads/{RELEASE} {head} {rel_old}\ncommit\n"))
        if r.returncode != 0:
            self.store.record_batch_step(bid, {"step": "promote", "ok": False})
            raise StepFailed("promote", f"{DEV} or {RELEASE} moved under the batch: "
                             f"{r.stderr.strip()[-500:]}")
        self.store.update_batch(bid, dev_after=head, release_after=head)
        moved = []
        for path, old in clean:
            if _git(path, "read-tree", "-m", "-u", old, head).returncode == 0:
                moved.append(str(path))
        self.store.record_batch_step(bid, {"step": "promote", "ok": True, "head": head,
                                           "checkouts": moved})

    def _backup_in_line(self, bid: str, bare: Path, remote: str, head: str) -> None:
        """Refuse a promote that the backup could not take by fast-forward."""
        r = _git(bare, "ls-remote", "--exit-code", remote, f"refs/heads/{RELEASE}")
        if r.returncode == 2:
            return
        if r.returncode != 0:
            self.store.record_batch_step(bid, {"step": "backup", "ok": False})
            raise StepFailed("backup", f"cannot read {RELEASE} on {remote}: "
                                       f"{r.stderr.strip()[-500:]}")
        tip = r.stdout.split()[0]
        if not (_rev(bare, tip) and _is_ancestor(bare, tip, head)):
            self.store.record_batch_step(bid, {"step": "backup", "ok": False, "tip": tip})
            raise StepFailed("backup", (
                f"{RELEASE} on {remote} is at {tip[:12]}, which this batch's {RELEASE} "
                f"does not contain. Nothing landed. Bring {remote} back in line with "
                f"{RELEASE} by hand. deploy never forces a push."))

    def _backup(self, bid: str, bare: Path, state: dict[str, str],
                cfg: DeployConfig) -> None:
        if not cfg.backup:
            return
        r = _git(bare, "push", "--porcelain", cfg.backup,
                 f"{state['head']}:refs/heads/{RELEASE}")
        self.store.record_batch_step(bid, {"step": "backup", "ok": r.returncode == 0,
                                           "remote": cfg.backup})
        if r.returncode != 0:
            raise StepFailed("backup", f"push of {RELEASE} to {cfg.backup} failed: "
                             f"{(r.stderr or r.stdout).strip()[-500:]}", landed=True)

    def _clean_checkouts(self, bare: Path, branches: tuple[str, ...]) -> list[tuple[Path, str]]:
        """The worktrees with one of ``branches`` checked out and no local change,
        each with the commit it shows."""
        found, path, head = [], None, ""
        for line in _git(bare, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
            if line.startswith("worktree "):
                path = Path(line[9:])
            elif line.startswith("HEAD "):
                head = line[5:]
            elif line.startswith("branch ") and line[7:] in (
                    f"refs/heads/{b}" for b in branches):
                found.append((path, head))
        return [(p, h) for p, h in found
                if p and p.is_dir() and not _git(p, "status", "--porcelain").stdout.strip()]

    # -- report and failure -------------------------------------------------

    async def _report(self, project: str, tickets: list[dict[str, Any]], bid: str,
                      mark: str) -> None:
        """Post each submitter's verdict to their scope's journal and their card (R26)."""
        for t in tickets:
            body = (f"{mark}\nDeploy ticket {t['id']}: {t['branch']} at "
                    f"{t['sha'][:12]}, batch {bid}.")
            try:
                await self.out.journal(project, t["scope"], body)
            except Exception as e:  # noqa: BLE001
                log.warning("report to %s/%s failed: %s", project, t["scope"], e)
            if t.get("card"):
                try:
                    await self.out.card_post(t["card"], body)
                except Exception as e:  # noqa: BLE001
                    log.warning("report to card %s failed: %s", t["card"], e)

    async def _fail(self, batch: dict[str, Any], tickets: list[dict[str, Any]],
                    step: str, error: str, *, cfg: DeployConfig | None = None,
                    landed: bool = False) -> None:
        bid, project = batch["id"], batch["project"]
        cfg = cfg or DeployConfig()
        branches = "\n".join(f"- {t['branch']} at {t['sha'][:12]} (scope {t['scope']}, "
                             f"ticket {t['id']})" for t in tickets)
        where = (("Release already holds these commits, but the backup remote does "
                  "not, and no node refreshed." if step == "backup" else
                  "Release already holds these commits. The nodes after the failed "
                  "one still run the old release.") if landed else
                 f"Nothing landed: {DEV} and {RELEASE} did not move.")
        prompt = (
            f"The deploy batch {bid} of `{project}` failed at `{step}`.\n\n{where}\n\n"
            f"The batch held these branches:\n{branches}\n\nThe error:\n\n{error[-3000:]}"
            f"\n\nTake every branch of the batch together. Merge them on one branch, fix "
            f"the failure, and run `routine deploy` from this scope to resubmit. Read the "
            f"batch with `routine deploys` id={bid}."
            + (f"\n\n{cfg.fixer_prompt}" if cfg.fixer_prompt else ""))
        submitters = sorted({t["caller"] for t in tickets if t["caller"]})
        esc = await escalate(
            self.out, peer=cfg.fixer_peer, project=project, scope=f"deploy-fix-{bid}",
            name=f"deploy-fix-{bid}", prompt=prompt,
            started_by=(f"deploy batch {project}/{bid}, submitted by "
                        f"{', '.join(submitters) or 'unknown callers'}"),
            summary=f"Deploy batch {project}/{bid} failed at {step}.")
        mark = f"✗ failed at {step}"
        self.store.finish_batch(bid, f"{step}-failed" if landed else "failed", mark=mark,
                                failed_step=step, error=error,
                                fixer=esc.job or esc.via)
        await self._report(project, tickets, bid,
                           f"{mark}. A fixing session took the batch "
                           f"({esc.via}{' ' + esc.job if esc.job else ''}).")
