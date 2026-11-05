"""Gateway entry point of the routine plugin: `python -m awm.routine.hub_adapter`.

Serves one tool, `routine` (routines.md R37, R39), with the deploy verbs of
R26 and R31 beside the routine verbs, over today's control
WebSocket protocol, which the Rust gateway keeps (plugin-contract.md § Service
wire protocol).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from awm.routine import fleetconf, timers
from awm.routine.deploy import Deployer
from awm.routine.outbound import GatewayOutbound, Outbound
from awm.routine.refresh import Refresher, default_checkout_root
from awm.routine.runner import Runner
from awm.routine.store import Store

log = logging.getLogger("awm.routine.hub_adapter")


def _p(name: str, type_: str, required: bool, description: str) -> dict[str, Any]:
    return {"name": name, "type": type_, "required": required, "description": description}


_PROJECT = _p("project", "string", True, "The project the routine belongs to.")
_NAME = _p("name", "string", True, "The routine's name.")

ROUTINE_FUNCTIONS: list[dict[str, Any]] = [
    {"name": "install", "tool": "routine_install", 
     "description": ("Install a routine on this node from its definition file in a "
                     "checkout of its project, pinned at the checkout's commit. "
                     "Reinstall after editing the definition."),
     "params": [_PROJECT, _p("path", "string", True,
                             "The definition file, such as <worktree>/routines/<name>.toml.")]},
    {"name": "uninstall", "tool": "routine_uninstall", 
     "description": "Remove an installed routine and its timer.",
     "params": [_PROJECT, _NAME]},
    {"name": "list", "tool": "routine_list", 
     "description": "The routines installed on this node, with their timers and last run.",
     "params": [_p("project", "string", False, "Only this project.")]},
    {"name": "trigger", "tool": "routine_trigger", 
     "description": ("Run a routine now and return its run record at once. Refused "
                     "while the routine has a live run. A failed run starts a fixing "
                     "agent on the routine's declared peer."),
     "params": [_PROJECT, _NAME,
                _p("wait", "boolean", False, "Return only when the run ends."),
                _p("trigger", "string", False, "`call` (default) or `timer`.")]},
    {"name": "cancel", "tool": "routine_cancel", 
     "description": "Stop a routine's live run and every process it started.",
     "params": [_PROJECT, _NAME]},
    {"name": "schedule", "tool": "routine_schedule", 
     "description": ("Set a routine's timer: a systemd user timer that calls "
                     "`trigger`. Uses the definition's timer unless `timer` is given."),
     "params": [_PROJECT, _NAME,
                _p("timer", "string", False, "A systemd OnCalendar expression.")]},
    {"name": "unschedule", "tool": "routine_unschedule", 
     "description": "Clear a routine's timer.",
     "params": [_PROJECT, _NAME]},
    {"name": "runs", "tool": "routine_runs", 
     "description": "Recent runs, newest first, or one run by id.",
     "params": [_p("project", "string", False, "Only this project."),
                _p("name", "string", False, "Only this routine."),
                _p("id", "string", False, "One run."),
                _p("limit", "integer", False, "How many. Default 20.")]},
]


def caller_label(args: dict[str, Any], as_: str | None) -> str:
    if as_:
        return str(as_)
    pid = args.get("_caller_pid")
    return f"pid {pid}" if pid else ""


class RoutineService:
    """The handlers, apart from the adapter so tests drive them directly."""

    def __init__(self, store: Store, out: Outbound, *,
                 systemctl: timers.Systemctl | None = None,
                 unit_dir: Path | None = None, on_usage=None) -> None:
        self.store = store
        self.out = out
        self.runner = Runner(store, out, on_usage=on_usage)
        self._systemctl = systemctl
        self._unit_dir = unit_dir

    def handlers(self) -> dict[str, Any]:
        return {
            "install": self.install, "uninstall": self.uninstall, "list": self.list,
            "trigger": self.trigger, "cancel": self.cancel, "schedule": self.schedule,
            "unschedule": self.unschedule, "runs": self.runs,
        }

    def install(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.runner.install(args["project"], args["path"])

    def uninstall(self, args: dict[str, Any]) -> dict[str, Any]:
        rec = self.store.get_routine(args["project"], args["name"])
        if rec and rec["scheduled"]:
            self.unschedule(args)
        return {"removed": self.runner.uninstall(args["project"], args["name"])}

    def list(self, args: dict[str, Any]) -> dict[str, Any]:
        out = []
        for rec in self.store.list_routines(args.get("project")):
            d = rec["definition"]
            last = self.store.list_runs(rec["project"], rec["name"], limit=1)
            out.append({
                "project": rec["project"], "name": rec["name"],
                "description": d.get("description", ""), "timer": d.get("timer", ""),
                "scheduled": bool(rec["scheduled"]), "commit": rec["commit_sha"][:12],
                "path": rec["path"],
                "last_run": ({k: last[0][k] for k in ("id", "state", "started_at",
                                                     "failed_step")} if last else None),
            })
        return {"routines": out}

    async def trigger(self, args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
        trig = args.get("trigger") or "call"
        if trig not in ("call", "timer"):
            raise ValueError("trigger must be `call` or `timer`")
        return await self.runner.trigger(args["project"], args["name"], trigger=trig,
                                         caller=caller_label(args, as_),
                                         wait=bool(args.get("wait")))

    async def cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self.runner.cancel(args["project"], args["name"])

    def schedule(self, args: dict[str, Any]) -> dict[str, Any]:
        _, routine = self.runner.routine(args["project"], args["name"])
        spec = args.get("timer") or routine.timer
        if not spec:
            raise ValueError("the routine defines no timer. Pass `timer`")
        res = timers.schedule(args["project"], args["name"], spec,
                              systemctl=self._systemctl, directory=self._unit_dir)
        self.store.set_scheduled(args["project"], args["name"], True)
        return res

    def unschedule(self, args: dict[str, Any]) -> dict[str, Any]:
        res = timers.unschedule(args["project"], args["name"],
                                systemctl=self._systemctl, directory=self._unit_dir)
        self.store.set_scheduled(args["project"], args["name"], False)
        return res

    def runs(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("id"):
            run = self.store.get_run(args["id"])
            if run is None:
                raise KeyError(f"no run {args['id']}")
            return run
        return {"runs": self.store.list_runs(args.get("project"), args.get("name"),
                                             int(args.get("limit") or 20))}


def _usage_hook():
    """The background usage event of plugin-contract.md § Usage event, when the
    helper library is installed."""
    try:
        from awm.usage import record_background  # type: ignore[import-not-found]
    except ImportError:
        return None
    return lambda job, ok, ms: record_background(job, ok, ms)


def build(store: Store | None = None, out: Outbound | None = None) -> RoutineService:
    return RoutineService(store or Store(), out or GatewayOutbound(),
                          on_usage=_usage_hook())


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from awm.gatewayclient import ServiceAdapter

    svc = build()
    orphans = svc.store.orphan_live()
    if orphans:
        log.warning("closed %d runs or batches left live by a previous process", orphans)
    deployer = build_deployer(svc.store, svc.out)
    manifest = {"functions": ROUTINE_FUNCTIONS + DEPLOY_FUNCTIONS, "emitters": [],
                "sessions": []}
    handlers = {**svc.handlers(), **DeployVerbs(deployer).handlers()}
    adapter = ServiceAdapter(os.environ.get("AWM_SERVICE_NAME") or "routine",
                             manifest, handlers)
    await asyncio.gather(adapter.run(), deployer.run_forever())


DEPLOY_FUNCTIONS: list[dict[str, Any]] = [
    {"name": "deploy", "tool": "routine_deploy", 
     "description": ("Land the calling scope's branch: pin its head, return a ticket at "
                     "once, then merge, check, promote to release, back it up and "
                     "refresh the nodes in a batch. The verdict goes to the scope's "
                     "journal and the calling session's card. A failed batch starts one "
                     "fixing session with every branch in it."),
     "params": [_p("project", "string", False, "The project. Default: the caller's."),
                _p("scope", "string", False, "The scope. Default: the caller's."),
                _p("branch", "string", False, "The branch. Default: the scope's."),
                _p("sha", "string", False, ("A commit to pin instead of the branch "
                                            "head, for a call from a peer.")),
                _p("card", "string", False, ("The Swarm-board task that takes the "
                                             "verdict, for a call from a peer. Default: "
                                             "the calling session's."))]},
    {"name": "deploys", "tool": "routine_deploys", 
     "description": "Recent deploy batches with their tickets, or one batch or ticket by id.",
     "params": [_p("project", "string", False, "Only this project."),
                _p("id", "string", False, "One batch or ticket."),
                _p("limit", "integer", False, "How many batches. Default 10.")]},
    {"name": "refresh", "tool": "routine_refresh", 
     "description": ("Refresh this node's checkout of a project after a deploy, from "
                     "its own deploy.toml. The deploy calls it on each node in turn."),
     "params": [_PROJECT, _p("from", "string", True, "The old release commit."),
                _p("to", "string", True, "The new release commit.")]},
]


class DeployVerbs:
    def __init__(self, deployer: Deployer) -> None:
        self.deployer = deployer

    def handlers(self) -> dict[str, Any]:
        return {"deploy": self.deploy, "deploys": self.deploys, "refresh": self.refresh}

    def canonical(self, project: str) -> str:
        """The node that lands ``project`` (R31), from the install config."""
        return self.deployer.refresher.fleet_config().canonical(project)

    async def deploy(self, args: dict[str, Any], as_: str | None = None) -> dict[str, Any]:
        cwd = ""
        card = str(args.get("card") or "")
        if pid := args.get("_caller_pid"):
            try:
                cwd = os.readlink(f"/proc/{int(pid)}/cwd")
            except (OSError, ValueError):
                cwd = ""
            if not card:
                card = await self._card_of(pid)
        sub = self.deployer.resolve(project=args.get("project") or "",
                                    scope=args.get("scope") or "",
                                    branch=args.get("branch") or "",
                                    sha=args.get("sha") or "", cwd=cwd)
        sub.card = card
        caller = caller_label(args, as_)
        home = self.canonical(sub.project)
        if home and home != self.deployer.refresher.node:
            # The canonical node pins the same commit, which the caller must have pushed.
            return await self.deployer.out.invoke(
                "routine", "deploy", {"project": sub.project, "scope": sub.scope,
                                      "branch": sub.branch, "sha": sub.sha, "card": card},
                peer=home)
        t = self.deployer.submit(sub, caller=caller)
        return {"ticket": t["id"], "project": t["project"], "scope": t["scope"],
                "branch": t["branch"], "sha": t["sha"],
                "note": "The verdict goes to this scope's journal and your card."}

    async def _card_of(self, pid: Any) -> str:
        """The caller's card, or "" when the board cannot say. A failed lookup
        never fails the deploy."""
        try:
            return await self.deployer.out.card_of(int(pid))
        except Exception as e:  # noqa: BLE001
            log.warning("no card for caller pid %s: %s", pid, e)
            return ""

    def deploys(self, args: dict[str, Any]) -> dict[str, Any]:
        store = self.deployer.store
        if bid := args.get("id"):
            if batch := store.get_batch(bid):
                return {**batch, "tickets": store.batch_tickets(bid)}
            if ticket := store.get_ticket(bid):
                return ticket
            raise KeyError(f"no batch or ticket {bid}")
        project = args.get("project")
        batches = store.list_batches(project, int(args.get("limit") or 10))
        return {"waiting": store.list_tickets(project, "queued"),
                "batches": [{**b, "tickets": store.batch_tickets(b["id"])}
                            for b in batches]}

    async def refresh(self, args: dict[str, Any]) -> dict[str, Any]:
        r = await self.deployer.refresher.local(args["project"], args["from"], args["to"])
        return r.to_dict()


def build_deployer(store: Store, out: Outbound) -> Deployer:
    from awm.config import AWM_DIR, canonical_workspace, node_name
    ws = canonical_workspace()
    install_config = Path(os.environ.get("AWM_INSTALL_CONFIG") or AWM_DIR / fleetconf.FILE)
    return Deployer(store, out, workspace=ws,
                    refresher=Refresher(out, node=node_name(),
                                        checkout_root=default_checkout_root(ws),
                                        fleet_config=lambda: fleetconf.load(install_config),
                                        artifact_store=AWM_DIR / "artifacts"))


if __name__ == "__main__":
    asyncio.run(main())
