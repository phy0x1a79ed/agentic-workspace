"""The two backup jobs, and the one path every submission goes through.

:mod:`awm.dvc.sync` and :mod:`awm.dvc.backup` know how to build and submit their
transfer documents and nothing else. This module is what turns either of them
into a *run*: it claims the job's single-flight slot, records the attempt, and
hands back a task id. Three processes submit — the in-service scheduler, the MCP
verb, and the ``awm-dvc-sync`` console script — and they all come through
:func:`run_job`, which is what makes run history complete rather than a partial
picture of whichever entry point someone remembered to instrument.

The job definitions are split deliberately. What a job *does* is code, here.
When it runs is state, in ``dvc_jobs`` (see :mod:`awm.dvc.runs`) — seeded from
the defaults below on first insert only, so an operator's schedule change is not
reverted by the next restart.

The two jobs are paired, not independent: the mirror excludes the DVC checkouts
because the cache sync uploads their bytes by the other path. Disabling
``cache_sync`` while ``workspace_backup`` keeps running turns those exclusions
from a deduplication into data loss. :func:`awm.dvc.coverage.report` is what
makes that visible; nothing here enforces it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from awm.dvc import backup as backupmod
from awm.dvc import globus
from awm.dvc import runs
from awm.dvc import sync as syncmod

log = logging.getLogger("awm.dvc.jobs")

CACHE_SYNC = "cache_sync"
WORKSPACE_BACKUP = "workspace_backup"


@dataclass(frozen=True)
class JobSpec:
    """A schedulable backup job: what it does, and what it defaults to."""

    name: str
    description: str
    label: str
    cron: str
    catchup_window_s: int
    submit: Callable[..., dict[str, Any]]
    busy_note: str  # formatted with {task}


JOBS: dict[str, JobSpec] = {
    CACHE_SYNC: JobSpec(
        name=CACHE_SYNC,
        description=(
            "Append-only push of the shared DVC cache to chinook. Never deletes "
            "on the remote, so a local `dvc gc` stays recoverable."
        ),
        label=syncmod.SYNC_LABEL,
        # Matches the OnCalendar the retiring systemd timer fired on.
        cron="0 4 * * *",
        # Long enough to cover a machine asleep overnight; short enough that a
        # week powered off does not fire a stale backup on boot.
        catchup_window_s=20 * 3600,
        submit=syncmod.sync,
        busy_note=(
            "sync task {task} is still running — a second full-cache scan would "
            "only duplicate its work. Pass force=true to submit anyway."
        ),
    ),
    WORKSPACE_BACKUP: JobSpec(
        name=WORKSPACE_BACKUP,
        description=(
            "Mirror of the whole workspace to chinook, minus the DVC checkouts "
            "and the cache. DELETES on the remote: a local deletion propagates."
        ),
        label=backupmod.BACKUP_LABEL,
        # Offset from the cache sync so the two pin scans do not overlap, and
        # kept clear of 01:00-03:00 where a DST shift makes a local-time cron
        # fire twice or not at all.
        cron="30 5 * * *",
        catchup_window_s=20 * 3600,
        submit=backupmod.backup,
        busy_note=(
            "backup task {task} is still running — a second whole-tree scan "
            "would only duplicate its work. Pass force=true to submit anyway."
        ),
    ),
}


def spec_for(name: str) -> JobSpec:
    try:
        return JOBS[name]
    except KeyError:
        raise ValueError(
            f"unknown job {name!r} — known jobs: {', '.join(sorted(JOBS))}"
        ) from None


def ensure_seeded(dao: runs.RunsDAO | None = None) -> None:
    """Create the ``dvc_jobs`` row for each job we know about, once."""
    runs.init()
    d = dao or runs.RunsDAO()
    for spec in JOBS.values():
        d.seed_job(spec.name, cron=spec.cron, catchup_window_s=spec.catchup_window_s)


def _busy_note(spec: JobSpec, live: dict | None) -> str:
    task = (live or {}).get("task_id") or ""
    if not task:
        return (
            f"a previous {spec.name} run is still being submitted — refusing to "
            "start a second one. Pass force=true to supersede it."
        )
    return spec.busy_note.format(task=task)


def run_job(
    name: str,
    *,
    trigger: str = "manual",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Submit ``name``'s transfer, recording the attempt as a run.

    Returns the job's own result dict plus ``run_id``. A declined run returns
    ``{"submitted": False, "in_flight": <task id>, "note": ...}`` — the shape
    the verb, the console script's "declining is success" branch, and the tests
    all read.

    A dry run touches neither table: it submits nothing, so there is nothing to
    hold a slot against or to reach a verdict.
    """
    spec = spec_for(name)
    if dry_run:
        return {**spec.submit(dry_run=True), "job": name}

    ensure_seeded()
    dao = runs.RunsDAO()

    if force:
        # The unique index would otherwise reject the insert. Retiring the old
        # row does NOT cancel its Globus task — that keeps running, and the
        # forced submit genuinely duplicates it. Which is what force means.
        closed = dao.close_live(name, note="superseded by a forced run")
        if closed:
            log.warning("%s: forced run superseded %d live row(s)", name, closed)

    run = dao.begin_run(name, trigger=trigger)
    if run is None:
        note = _busy_note(spec, dao.live_run(name))
        dao.record_skip(name, trigger=trigger, note=note)
        log.info("%s: declined (%s)", name, note)
        return {
            "submitted": False,
            "job": name,
            "in_flight": (dao.live_run(name) or {}).get("task_id", ""),
            "note": note,
        }

    try:
        result = spec.submit()
    except Exception as exc:  # noqa: BLE001 — the row is the error report
        dao.fail(run["id"], f"{type(exc).__name__}: {exc}")
        log.error("%s: submit failed: %s", name, exc)
        raise

    task_id = str(result.get("task_id") or "")
    if not task_id:
        # Shouldn't happen: a non-dry submit either returns an id or raises.
        dao.fail(run["id"], "submit returned no task id")
        return {**result, "job": name, "run_id": run["id"]}

    dao.mark_submitted(run["id"], task_id, label=spec.label)
    log.info("%s: submitted task %s (trigger=%s)", name, task_id, trigger)
    return {**result, "job": name, "run_id": run["id"], "trigger": trigger}


def record_status(dao: runs.RunsDAO, run_id: str, status_doc: dict) -> str:
    """Fold a Globus status document into a run row; return the row's status.

    A terminal document closes the row out with its final counters; anything
    else is recorded as progress and leaves the row live — which is exactly what
    lets a timed-out console script hand its transfer to the service's adopt
    sweep rather than abandoning it.
    """
    status = str(status_doc.get("status") or "")
    if status_doc.get("done") and status in globus.TERMINAL:
        dao.finish(run_id, status, status_doc)
        return status
    dao.record_poll(run_id, status_doc)
    return "running"
