"""Install, trigger, cancel and watch routine runs (routines.md R34 to R38, R47, R48)."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from awm.routine import definitions, proc
from awm.routine.definitions import Routine, Step
from awm.routine.escalate import escalate, notice
from awm.routine.outbound import Outbound
from awm.routine.store import LiveRunExists, Store

log = logging.getLogger("awm.routine.runner")

UsageHook = Callable[[str, bool, int], None]


class Runner:
    def __init__(self, store: Store, out: Outbound, *,
                 on_usage: UsageHook | None = None) -> None:
        self.store = store
        self.out = out
        self.on_usage = on_usage
        self._tasks: dict[str, asyncio.Task] = {}

    # -- registry ---------------------------------------------------------

    def install(self, project: str, path: str) -> dict[str, Any]:
        """Install the routine defined at ``path``, a file inside a checkout of
        ``project``. The definition is stored as read now. An edit needs a
        reinstall, so a run never picks up a half-written file."""
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"no routine definition at {p}")
        routine = definitions.load(p)
        try:
            root = proc.git(str(p.parent), "rev-parse", "--show-toplevel")
            sha = proc.git(root, "rev-parse", "HEAD")
            dirty = bool(proc.git(root, "status", "--porcelain", "--", str(p)))
        except subprocess.CalledProcessError:
            root, sha, dirty = str(p.parent), "", False
        if dirty:
            raise ValueError(f"{p} has uncommitted changes. Commit the definition first")
        rec = self.store.put_routine(project=project, name=routine.name, root=root,
                                     path=str(p), commit_sha=sha,
                                     definition=routine.to_dict())
        return _public(rec)

    def uninstall(self, project: str, name: str) -> bool:
        if self.store.live_run(project, name):
            raise LiveRunExists(f"{project}/{name} has a live run. Cancel it first")
        return self.store.delete_routine(project, name)

    def routine(self, project: str, name: str) -> tuple[dict[str, Any], Routine]:
        rec = self.store.get_routine(project, name)
        if rec is None:
            raise KeyError(f"no routine {project}/{name} is installed on this node")
        return rec, Routine.from_dict(rec["definition"])

    # -- runs -------------------------------------------------------------

    async def trigger(self, project: str, name: str, *, trigger: str = "call",
                      caller: str = "", wait: bool = False) -> dict[str, Any]:
        rec, routine = self.routine(project, name)
        if trigger == "call" and not routine.call:
            raise ValueError(f"{project}/{name} runs on its timer only")
        run = self.store.start_run(project=project, name=name, trigger=trigger,
                                   caller=caller)
        task = asyncio.create_task(self._execute(rec, routine, run))
        self._tasks[run["id"]] = task
        task.add_done_callback(lambda _t, rid=run["id"]: self._tasks.pop(rid, None))
        if wait:
            await asyncio.shield(task)
            return self.store.get_run(run["id"])  # type: ignore[return-value]
        return run

    async def cancel(self, project: str, name: str) -> dict[str, Any]:
        live = self.store.live_run(project, name)
        if live is None:
            return {"cancelled": False, "reason": "no live run"}
        task = self._tasks.get(live["id"])
        if task is None:
            # A run with no task here belongs to a dead process.
            self.store.finish_run(live["id"], "cancelled", error="no task held the run")
        else:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # A task cancelled before its first step never reached its handler.
            self.store.finish_run(live["id"], "cancelled", error="cancelled by call")
        return {"cancelled": True, "run": self.store.get_run(live["id"])}

    async def drain(self) -> None:
        """Wait for every live run task. For tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def _execute(self, rec: dict[str, Any], routine: Routine,
                       run: dict[str, Any]) -> None:
        rid, project = run["id"], rec["project"]
        env = {"AWM_ROUTINE": routine.name, "AWM_ROUTINE_PROJECT": project,
               "AWM_ROUTINE_RUN": rid, "AWM_ROUTINE_TRIGGER": run["trigger"]}
        t0 = time.monotonic()
        ok = False
        try:
            for step in routine.steps:
                res = await proc.run(step.run, cwd=rec["root"], timeout=step.timeout,
                                     env=env)
                outcome = {"step": step.name, "kind": step.kind, "code": res.code,
                           "timed_out": res.timed_out, "seconds": res.seconds}
                if step.kind == "condition" and res.code == 0 and not res.timed_out:
                    outcome["held"] = True
                    self.store.record_step(rid, outcome)
                    await self._dispatch_agent(rec, routine, step, run, res.output)
                    continue
                if step.kind == "condition" and res.code == 1:
                    outcome["held"] = False
                    self.store.record_step(rid, outcome)
                    continue
                self.store.record_step(rid, outcome)
                if not res.ok:
                    why = (f"timed out after {step.timeout:g}s" if res.timed_out
                           else f"exited {res.code}")
                    self.store.finish_run(rid, "failed", failed_step=step.name,
                                          error=f"{why}\n{res.output}")
                    await self._dispatch_fixer(rec, routine, run, step, why, res.output)
                    return
            ok = True
            self.store.finish_run(rid, "ok")
        except asyncio.CancelledError:
            self.store.finish_run(rid, "cancelled", error="cancelled by call")
            raise
        except Exception as e:  # noqa: BLE001 — an unexpected error is a failed run
            log.exception("run %s crashed", rid)
            self.store.finish_run(rid, "failed", failed_step="(service)", error=repr(e))
            await self._dispatch_fixer(rec, routine, run, None, "the runner raised",
                                       repr(e))
        finally:
            if self.on_usage:
                try:
                    self.on_usage(f"{project}/{routine.name}", ok,
                                  int((time.monotonic() - t0) * 1000))
                except Exception:  # noqa: BLE001
                    log.debug("usage hook failed", exc_info=True)

    def _started_by(self, rec: dict[str, Any], run: dict[str, Any]) -> str:
        who = run["caller"] or f"its {run['trigger']} trigger"
        return f"routine {rec['project']}/{rec['name']} run {run['id']}, started by {who}"

    async def _dispatch_agent(self, rec: dict[str, Any], routine: Routine, step: Step,
                              run: dict[str, Any], output: str) -> None:
        brief = step.prompt or routine.agent.prompt
        prompt = (f"{brief}\n\nThe routine `{rec['project']}/{routine.name}` (run "
                  f"{run['id']}) found that the condition `{step.name}` holds. "
                  f"Its output:\n\n{output[-2000:]}")
        esc = await escalate(
            self.out, peer=routine.agent.peer, project=rec["project"],
            scope=routine.agent.scope or routine.name,
            name=f"{routine.name}-{run['id']}", prompt=prompt,
            started_by=self._started_by(rec, run),
            summary=f"Routine {rec['project']}/{routine.name}: condition {step.name} holds.")
        self.store.record_dispatch(run["id"], {"for": step.name, "role": "agent",
                                               **esc.to_dict()})

    async def _dispatch_fixer(self, rec: dict[str, Any], routine: Routine,
                              run: dict[str, Any], step: Step | None, why: str,
                              output: str) -> None:
        sname = step.name if step else "(service)"
        summary = (f"Routine {rec['project']}/{routine.name} failed at {sname} "
                   f"(run {run['id']}): {why}.")
        if routine.fixer is None:
            esc = await notice(self.out, f"{summary}\n```\n{output[-1500:]}\n```")
            self.store.record_dispatch(run["id"], {"for": sname, "role": "notice",
                                                   **esc.to_dict()})
            return
        prompt = (
            f"The routine `{rec['project']}/{routine.name}` failed (run {run['id']}). "
            f"Step `{sname}` {why}.\n\nIts output:\n\n{output[-3000:]}\n\n"
            f"The definition is `{rec['path']}`, installed at commit "
            f"{rec['commit_sha'][:12] or 'unknown'}. Find the cause and fix it. Then run "
            f"`routine trigger` with project={rec['project']} name={routine.name} "
            f"to confirm the fix. Read the run with `routine runs`."
            + (f"\n\n{routine.fixer.prompt}" if routine.fixer.prompt else ""))
        esc = await escalate(
            self.out, peer=routine.fixer.peer, project=rec["project"],
            scope=routine.fixer.scope or f"fix-{routine.name}",
            name=f"fix-{routine.name}-{run['id']}", prompt=prompt,
            started_by=self._started_by(rec, run), summary=summary)
        self.store.record_dispatch(run["id"], {"for": sname, "role": "fixer",
                                               **esc.to_dict()})


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    d = dict(rec)
    d["scheduled"] = bool(d.get("scheduled"))
    return d

