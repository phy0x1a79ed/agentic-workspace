"""Hub adapter for the dvc service — DVC cache sync against the chinook collection.

Boots dvc as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve → reconnect).
The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

On the collapsed MCP surface this is a single ``dvc`` domain tool
(``dvc(verb="status")``, ``verb="pull"``, …); CLI and HTTP stay expanded as
``dvc_<verb>`` (``awm dvc pull``, ``POST /invoke {name:"dvc_pull"}``).

Three jobs, one config:
  - ``sync`` — the daily append-only push of the whole shared cache.
  - ``status`` / ``resolve`` / ``pull`` / ``push`` — the hash-selective inverse,
    moving only the objects one scope actually pins.
  - ``coverage`` — what the two remotes between them do *not* hold.

The two scheduled backups run inside this process: ``on_start`` attaches
:class:`awm.dvc.scheduler.Scheduler` under ``spawn_supervised``, and ``jobs`` /
``runs`` / ``run`` / ``schedule`` are its control surface. ``on_start`` itself
only touches SQLite — no Globus call, because a service that stays unready gets
killed by the gateway's orphan reaper.

The byte-moving verbs address the same tree on chinook, so they read one
``prefix`` from ``$AWM_DIR/dvc.toml``. They must agree: ``pull`` reads back
exactly what ``sync`` wrote, and a mismatch means a restore silently finds
nothing.

Every verb that moves bytes **submits and returns a task id** rather than
blocking — a Globus task runs for minutes to hours. Poll with ``task``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.dvc.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from time import time
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.dvc import coverage as coveragemod
from awm.dvc import cron, jobs, runs, transfer
from awm.dvc.config import load as load_config
from awm.dvc.globus import task_status, wait
from awm.dvc.scheduler import Scheduler

log = logging.getLogger("awm.dvc.hub_adapter")

# Write verbs stay off the MCP surface: an agent reads backup health, a human
# changes when the backups run.
_CLI_HTTP = ["cli", "http"]

_adapter: ServiceAdapter | None = None
_scheduler: Scheduler | None = None

_SCOPE_PARAM = {
    "name": "scope",
    "type": "string",
    "required": True,
    "description": (
        "Scope worktree — absolute, or relative to the workspace root "
        "(e.g. 'projects/fabfos/dev')."
    ),
}

_DRY_RUN_PARAM = {
    "name": "dry_run",
    "type": "boolean",
    "required": False,
    "description": "Return the transfer items that would be submitted, and submit nothing.",
}

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "dvc_status",
            "description": (
                "Report a scope's DVC pin state against the local cache: pin count, "
                ".dir manifests, and how many objects are present vs missing. Local "
                "only — touches no network. unresolved_manifests>0 means some .dir "
                "manifests are absent, so their leaves are not yet counted."
            ),
            "params": [_SCOPE_PARAM],
            "timeout": 300.0,
        },
        {
            "name": "resolve",
            "tool": "dvc_resolve",
            "description": (
                "List the exact object hashes a scope depends on, split into "
                "present / missing / unresolved_manifests. Local only. Use to see "
                "precisely what a pull would move."
            ),
            "params": [_SCOPE_PARAM],
            "timeout": 300.0,
        },
        {
            "name": "pull",
            "tool": "dvc_pull",
            "description": (
                "Fetch from chinook only the cache objects this scope pins and does "
                "not have. Submits a Globus task and returns its task_id; poll with "
                "task. A cold restore is two-phase — .dir manifests must land before "
                "their leaves can be named — so a call whose phase is 'manifests' "
                "should be repeated once that task completes to fetch the leaves. "
                "Safe to repeat: each call re-resolves and does whatever remains. "
                "After the final task, run `dvc checkout` in the scope to materialize."
            ),
            "params": [_SCOPE_PARAM, _DRY_RUN_PARAM],
            "timeout": 600.0,
        },
        {
            "name": "push",
            "tool": "dvc_push",
            "description": (
                "Send the cache objects this scope pins up to chinook. Submits a "
                "Globus task and returns its task_id; poll with task. Never deletes "
                "on the remote. Fails if any .dir manifest is missing locally — you "
                "cannot push what you do not have."
            ),
            "params": [_SCOPE_PARAM, _DRY_RUN_PARAM],
            "timeout": 600.0,
        },
        {
            "name": "sync",
            "tool": "dvc_sync",
            "description": (
                "Submit the daily sync of the shared DVC cache (data/.dvc_cache) to "
                "chinook and return its task_id. APPEND-ONLY: never deletes on the "
                "remote, so chinook accumulates objects and a local `dvc gc` stays "
                "recoverable. Covers every project's data at once, because every "
                "project shares that one cache. Declines if a previous sync is still "
                "running unless force=true. Nothing else in the workspace is backed "
                "up by this — see coverage."
            ),
            "params": [
                {
                    "name": "dry_run",
                    "type": "boolean",
                    "required": False,
                    "description": "Build and return the transfer document; submit nothing.",
                },
                {
                    "name": "force",
                    "type": "boolean",
                    "required": False,
                    "description": "Submit even if a previous sync task is still running.",
                },
            ],
            "timeout": 300.0,
        },
        {
            "name": "coverage",
            "tool": "dvc_coverage",
            "description": (
                "Report what would be lost if this machine died: per scope worktree, "
                "uncommitted and untracked files, branches with no upstream or ahead "
                "of one, and pins whose objects are absent from the shared cache. "
                "Read-only, no network. Code is covered by GitHub and data by the "
                "cache sync; everything else is disposable by decision, and this is "
                "how that decision is checked."
            ),
            "params": [
                {
                    "name": "all",
                    "type": "boolean",
                    "required": False,
                    "description": "Include scopes with nothing at risk (default: only at-risk ones).",
                },
            ],
            # A `git status` per worktree across the whole workspace.
            "timeout": 900.0,
        },
        {
            "name": "task",
            "tool": "dvc_task",
            "description": (
                "Status of a Globus task submitted by this service: state, files "
                "transferred, bytes, throughput, faults. Pass wait=<seconds> to block "
                "until it finishes or that budget runs out (timed_out=true means "
                "still running, not failed). nice_status names the current fault, if "
                "any; an ACTIVE task with faults is still retrying."
            ),
            "params": [
                {"name": "task_id", "type": "string", "required": True},
                {
                    "name": "wait",
                    "type": "integer",
                    "required": False,
                    "description": "Seconds to block waiting for a terminal state (default: return immediately).",
                },
            ],
            "timeout": 3600.0,
        },
        {
            "name": "jobs",
            "tool": "dvc_jobs",
            "description": (
                "The two scheduled backups — cache_sync (append-only archive of "
                "the DVC cache) and workspace_backup (mirror of everything else, "
                "which DELETES on the remote) — with their cron, enabled state, "
                "next due time, and last outcome. Also reports the scheduler "
                "loop's own health: stopped=true or a large last_tick_age_s "
                "means nothing is firing, which is how a backup silently stops "
                "happening. Read-only."
            ),
            "params": [],
            "timeout": 30.0,
        },
        {
            "name": "runs",
            "tool": "dvc_runs",
            "description": (
                "Backup run history — one row per attempt, from every entry "
                "point (scheduler, manual verb, awm-dvc-sync). Carries task_id, "
                "trigger, status, file and byte counters, faults, and error "
                "text. Status is submitting/running, then SUCCEEDED or FAILED "
                "verbatim from Globus, or skipped (declined as already in "
                "flight) or error (never reached Globus). Read-only."
            ),
            "params": [
                {"name": "job", "type": "string", "required": False,
                 "description": "Filter to one job (cache_sync, workspace_backup)."},
                {"name": "limit", "type": "integer", "required": False,
                 "description": "Rows to return, newest first (default 20)."},
                {"name": "status", "type": "string", "required": False},
                {"name": "since", "type": "string", "required": False,
                 "description": "Only runs started at or after this ISO-8601 time or epoch second."},
            ],
            "timeout": 60.0,
        },
        {
            "name": "run",
            "tool": "dvc_run",
            "description": (
                "Trigger a backup job now, out of schedule. Submits and returns "
                "a task_id; the service watches it to a verdict recorded in "
                "runs. Declines if that job is already in flight — a second "
                "whole-tree scan only duplicates the first — unless force=true, "
                "which does not cancel the running task."
            ),
            "params": [
                {"name": "job", "type": "string", "required": True,
                 "description": "cache_sync or workspace_backup."},
                {"name": "force", "type": "boolean", "required": False},
                _DRY_RUN_PARAM,
            ],
            "timeout": 300.0,
            "surfaces": _CLI_HTTP,
        },
        {
            "name": "schedule",
            "tool": "dvc_schedule",
            "description": (
                "Change when a backup job runs, or disable it. Accepts a 5-field "
                "cron or '@every <n><s|m|h>'. Takes effect within one scheduler "
                "tick, no restart. Disabling cache_sync while workspace_backup "
                "stays enabled is data loss, not a saving: the mirror excludes "
                "the DVC checkouts precisely because the archive holds them."
            ),
            "params": [
                {"name": "job", "type": "string", "required": True},
                {"name": "cron", "type": "string", "required": False},
                {"name": "enabled", "type": "boolean", "required": False},
            ],
            "timeout": 30.0,
            "surfaces": _CLI_HTTP,
        },
    ],
    "emitters": [
        {
            "topic": "job.status",
            "description": (
                "Fires on each backup run transition. Payload includes "
                "{run_id, job, trigger, status, task_id, timestamp, elapsed_s, "
                "message}, plus files / files_transferred / bytes_transferred / "
                "faults once a Globus task exists. Statuses: submitted -> "
                "progress* -> succeeded | failed, or skipped | error | stalled "
                "(stalled is an alert, not a verdict — the run keeps going). "
                "Best-effort: events sent while nothing is subscribed are lost, "
                "so read runs first and then tail this."
            ),
        },
    ],
    "sessions": [],
}


def _task(args: dict) -> dict:
    cfg = load_config()
    seconds = args.get("wait")
    if seconds:
        return wait(cfg, args["task_id"], timeout=int(seconds))
    return task_status(cfg, args["task_id"])


def _run_view(row: dict | None) -> dict | None:
    """A run row with the epoch stamps spelled out, for a human reader."""
    if not row:
        return None
    out = dict(row)
    for field in ("started_at", "submitted_at", "polled_at", "finished_at"):
        out[f"{field}_iso"] = runs.iso(row.get(field))
    return out


def _jobs(_args: dict) -> dict:
    dao = runs.RunsDAO()
    jobs.ensure_seeded(dao)
    now = time()
    out = []
    for row in dao.list_jobs():
        spec = jobs.JOBS.get(row["name"])
        # Prefer the loop's own figure; fall back to computing it so a verb
        # called before the first tick still answers rather than showing null.
        due = _scheduler.next_due(row["name"]) if _scheduler else None
        if due is None and row["enabled"]:
            try:
                due = cron.next_due(str(row["cron"]), after=now)
            except cron.CronError:
                due = None
        out.append({
            "name": row["name"],
            "description": spec.description if spec else "",
            "cron": row["cron"],
            "enabled": bool(row["enabled"]),
            "catchup_window_s": row["catchup_window_s"],
            "last_fire_at_iso": runs.iso(row["last_fire_at"]),
            "next_due": due,
            "next_due_iso": runs.iso(due),
            "last_run": _run_view(dao.last_run(row["name"])),
        })
    health = _scheduler.health() if _scheduler else {
        "last_tick_age_s": None, "ticks": 0, "watching": 0, "stopped": True,
    }
    return {"scheduler": health, "jobs": out}


def _runs(args: dict) -> dict:
    runs.init()
    dao = runs.RunsDAO()
    rows = dao.history(
        job=(args.get("job") or None),
        limit=int(args.get("limit") or 20),
        status=(args.get("status") or None),
        since=runs.to_epoch(args.get("since")),
    )
    return {"count": len(rows), "runs": [_run_view(r) for r in rows]}


def _run(args: dict) -> dict:
    return jobs.run_job(
        str(args["job"]).strip(),
        trigger="manual",
        force=bool(args.get("force")),
        dry_run=bool(args.get("dry_run")),
    )


def _schedule(args: dict) -> dict:
    name = str(args["job"]).strip()
    jobs.spec_for(name)  # reject an unknown job before writing anything
    spec = args.get("cron")
    if spec is not None:
        # Validated here rather than at fire time: a bad cron accepted now is a
        # job that silently stops being scheduled and says so only in a log.
        cron.validate(str(spec))
    dao = runs.RunsDAO()
    jobs.ensure_seeded(dao)
    row = dao.set_schedule(
        name,
        cron=(str(spec) if spec is not None else None),
        enabled=(bool(args["enabled"]) if "enabled" in args else None),
    )
    return {"job": row}


HANDLERS = {
    "status":  lambda a: transfer.status(a["scope"]),
    "resolve": lambda a: transfer.resolve(a["scope"]),
    "pull":    lambda a: transfer.pull(a["scope"], dry_run=bool(a.get("dry_run"))),
    "push":    lambda a: transfer.push(a["scope"], dry_run=bool(a.get("dry_run"))),
    # Routed through run_job so a hand-triggered sync lands in history and is
    # watched to a verdict, exactly like a scheduled one.
    "sync":    lambda a: jobs.run_job(
                   jobs.CACHE_SYNC,
                   trigger="manual",
                   dry_run=bool(a.get("dry_run")),
                   force=bool(a.get("force")),
               ),
    "coverage": lambda a: coveragemod.report(at_risk_only=not bool(a.get("all"))),
    "task":    _task,
    "jobs":     _jobs,
    "runs":     _runs,
    "run":      _run,
    "schedule": _schedule,
}


async def _on_start() -> None:
    """Stand up the DB and attach the scheduler. No network call belongs here."""
    global _scheduler
    await asyncio.to_thread(runs.init)
    await asyncio.to_thread(jobs.ensure_seeded)
    _scheduler = Scheduler(_adapter)
    spawn_supervised("dvc-scheduler", _scheduler.run)


async def main() -> None:
    global _adapter
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _adapter = ServiceAdapter("dvc", API_MANIFEST, HANDLERS, on_start=_on_start)
    await _adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
