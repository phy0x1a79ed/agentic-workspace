"""The cadence engine — fires the backup jobs, then watches them to a verdict.

This replaces a systemd user timer, and the two things that timer gave us for
free are the two things this loop has to reimplement.

**Catch-up.** ``Persistent=true`` runs a job missed while the machine was
asleep. Here that is ``last_fire_at`` plus a bounded window: on the first
reconcile after a restart, a slot missed *inside* the window fires once. One
fire, never a replay, and nothing at all if the machine was off for a week.

**Independence — which is genuinely lost.** The timer ran whether or not awm was
healthy; this loop does not. That is a real widening of the failure surface,
mitigated by the catch-up window, by the gateway being systemd-managed itself,
and by ``awm-dvc-sync`` remaining a gateway-free escape hatch. It is not
eliminated.

WHY THE LOOP MUST NEVER RETURN
It is attached with :func:`awm.gatewayclient.spawn_supervised`, which treats a
clean return as a defect just as loudly as an exception — because a scheduler
that quietly stopped is exactly how a backup silently stops happening. Every
error path here therefore logs and skips the tick. It is attached that way
rather than as an ``asyncio.gather`` sibling for the mirror-image reason: a
gathered task's exception kills the process, which burns the gateway's respawn
budget and can end at ``breaker-tripped``, where no dvc verb works and nothing
backs up.

THE ADOPT SWEEP IS THE RECOVERY MECHANISM
Every tick, any run row still marked live that no in-process watcher owns gets
one attached. That single rule covers a service restart mid-transfer, a killed
console script, a manual verb call (which otherwise reaches no verdict at all),
and a row orphaned between its insert and its submit. Adoption never resubmits:
the task id is authoritative, and ``globus.submit`` mints a fresh submission id
per call, so a "retry" would genuinely duplicate a running multi-hour transfer.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from time import time
from typing import Any

from awm.dvc import cron, globus, jobs, runs
from awm.dvc.config import load as load_config

log = logging.getLogger("awm.dvc.scheduler")

TOPIC = "job.status"

POLL_MAX_S = 5.0        # cap on the reconcile sleep, so a schedule edit lands fast
JITTER_S = 60.0         # pre-submit spread; a fleet must not hit one collection at 04:00
WATCH_POLL_S = 30       # Globus has no push, so a verdict costs a poll
PROGRESS_EMIT_S = 120   # floor for re-emitting unchanged counters
ORPHAN_AFTER_S = 300    # a live row with no task id this long never got submitted
STALL_AFTER_S = 26 * 3600  # a daily job still running past the next one's slot

# Consecutive unqueryable polls before a watcher gives up on the task. At
# WATCH_POLL_S that is five minutes — long enough to ride out a token refresh or
# a Globus API blip, short enough that a genuinely dead task gets a verdict.
_MAX_POLL_FAILURES = 10


class Scheduler:
    """Fires ``dvc_jobs`` on their crons and watches every live run."""

    def __init__(
        self,
        adapter: Any = None,
        *,
        dao: runs.RunsDAO | None = None,
        jitter_s: float = JITTER_S,
    ) -> None:
        self.adapter = adapter
        self.jitter_s = jitter_s
        self.dao = dao or runs.RunsDAO()
        # job -> next due epoch, and the spec it was computed from (change detect).
        self._next_due: dict[str, float] = {}
        self._specs: dict[str, str] = {}
        # run id -> watcher task.
        self._watching: dict[str, asyncio.Task] = {}
        self._caught_up = False
        self._ticks = 0
        self._last_tick = 0.0

    # -- health -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """What the ``jobs`` verb reports about the loop itself.

        A scheduler that stopped ticking is how a backup silently stops
        happening, so its liveness is part of the answer to "is this healthy?",
        not something to be inferred from the absence of runs.
        """
        age = time() - self._last_tick if self._last_tick else None
        alive = sum(1 for t in self._watching.values() if not t.done())
        return {
            "last_tick_age_s": round(age, 1) if age is not None else None,
            "ticks": self._ticks,
            "watching": alive,
            "tz": datetime.now().astimezone().tzname(),
            # The loop sleeps at most POLL_MAX_S, so a minute of silence is not
            # slowness; it is a loop that is gone.
            "stopped": age is None or age > 60,
        }

    def next_due(self, job: str) -> float | None:
        return self._next_due.get(job)

    # -- reconcile (pure, unit-testable, no sleeping) ------------------------

    def _catchup(self, rows: dict[str, dict], now: float) -> list[tuple[str, str]]:
        """Slots missed while the service was down, inside each job's window."""
        due: list[tuple[str, str]] = []
        for name, row in rows.items():
            last = float(row.get("last_fire_at") or 0)
            window = float(row.get("catchup_window_s") or 0)
            if not last or window <= 0:
                continue  # never fired, or catch-up disabled — nothing to make up
            try:
                missed = cron.next_due(str(row["cron"]), after=last)
            except cron.CronError:
                continue
            if missed <= now and (now - missed) <= window:
                log.info(
                    "dvc: %s missed its %s slot while down — catching up",
                    name,
                    runs.iso(missed),
                )
                due.append((name, "catchup"))
        return due

    def _reconcile(self, now: float) -> list[tuple[str, str]]:
        """Sync in-memory next-dues to ``dvc_jobs`` and return what is due.

        Re-read every pass so a ``schedule`` mutation — which arrives on a
        worker thread — is picked up without cross-thread signalling. Next-due
        is recomputed from the spec at fire time rather than by adding a period,
        so a loop that wakes late fires once and skips ahead instead of
        replaying every slot it slept through.
        """
        rows = {r["name"]: r for r in self.dao.list_jobs() if r["enabled"]}

        due: list[tuple[str, str]] = []
        if not self._caught_up:
            self._caught_up = True
            due.extend(self._catchup(rows, now))

        for gone in set(self._next_due) - set(rows):
            self._next_due.pop(gone, None)
            self._specs.pop(gone, None)

        for name, row in rows.items():
            spec = str(row["cron"] or "")
            if name not in self._next_due or self._specs.get(name) != spec:
                try:
                    self._next_due[name] = cron.next_due(spec, after=now)
                    self._specs[name] = spec
                except cron.CronError as exc:
                    log.warning("dvc: bad cron for %s (%r): %s", name, spec, exc)
                    self._next_due.pop(name, None)
                    self._specs.pop(name, None)

        for name, when in list(self._next_due.items()):
            if when <= now:
                due.append((name, "schedule"))
                try:
                    self._next_due[name] = cron.next_due(self._specs[name], after=now)
                except cron.CronError:
                    self._next_due.pop(name, None)
                    self._specs.pop(name, None)
        return due

    def _sleep_for(self, now: float) -> float:
        if not self._next_due:
            return POLL_MAX_S
        return max(0.0, min(POLL_MAX_S, min(self._next_due.values()) - now))

    # -- emit ---------------------------------------------------------------

    async def _emit(
        self,
        status: str,
        *,
        job: str,
        run_id: str = "",
        task_id: str = "",
        trigger: str = "",
        started_at: float = 0.0,
        message: str = "",
        doc: dict | None = None,
    ) -> None:
        """Best-effort live signal. Always AFTER the DB write, never instead of it.

        ``adapter.emit`` is a silent no-op while the control WS is down, so an
        event is a convenience for a subscriber that is already listening — the
        durable record is the run row, and the documented client pattern is to
        read history once and then tail.
        """
        if self.adapter is None:
            return
        payload: dict[str, Any] = {
            "run_id": run_id,
            "job": job,
            "trigger": trigger,
            "status": status,
            "task_id": task_id,
            "timestamp": time(),
            "elapsed_s": round(time() - started_at, 1) if started_at else None,
            "message": message,
        }
        if doc:
            for k in ("files", "files_transferred", "files_skipped",
                      "bytes_transferred", "faults", "nice_status"):
                payload[k] = doc.get(k)
        try:
            await self.adapter.emit(TOPIC, payload)
        except Exception as exc:  # noqa: BLE001 — signalling must never break a run
            log.debug("dvc: emit failed: %s", exc)

    # -- firing -------------------------------------------------------------

    async def _fire(self, name: str, trigger: str) -> None:
        now = time()
        # Stamped before the jitter sleep and before the submit, so a restart
        # mid-fire cannot make catch-up replay the same slot.
        await asyncio.to_thread(self.dao.set_last_fire, name, now)
        if self.jitter_s > 0:
            await asyncio.sleep(random.uniform(0, self.jitter_s))

        try:
            result = await asyncio.to_thread(
                jobs.run_job, name, trigger=trigger
            )
        except Exception as exc:  # noqa: BLE001 — run_job already recorded the row
            log.error("dvc: %s failed to submit: %s", name, exc)
            await self._emit(
                "error", job=name, trigger=trigger, started_at=now, message=str(exc)
            )
            return

        run_id = str(result.get("run_id") or "")
        if not result.get("submitted"):
            await self._emit(
                "skipped",
                job=name,
                trigger=trigger,
                started_at=now,
                task_id=str(result.get("in_flight") or ""),
                message=str(result.get("note") or ""),
            )
            return

        run = await asyncio.to_thread(self.dao.get_run, run_id)
        if run is None:
            return
        await self._emit(
            "submitted",
            job=name,
            run_id=run_id,
            trigger=trigger,
            task_id=run["task_id"],
            started_at=run["started_at"],
            message=f"submitted {run['task_id']}",
        )
        self._start_watch(run)

    # -- watching + adoption -------------------------------------------------

    def _start_watch(self, run: dict) -> None:
        existing = self._watching.get(run["id"])
        if existing is not None and not existing.done():
            return
        self._watching[run["id"]] = asyncio.create_task(
            self._watch(dict(run)), name=f"dvc-watch:{run['id'][:8]}"
        )

    async def _adopt_sweep(self) -> None:
        """Attach a watcher to every live run that has none. See the docstring."""
        live = await asyncio.to_thread(self.dao.live_runs)
        now = time()
        for run in live:
            task = self._watching.get(run["id"])
            if task is not None and not task.done():
                continue
            if not run["task_id"]:
                # Inserted, then the process died before `globus submit`
                # returned — or before it was even called. Nothing to adopt.
                if now - float(run["started_at"] or 0) > ORPHAN_AFTER_S:
                    msg = (
                        "orphaned: the run claimed the slot but no Globus task "
                        "was ever recorded against it, so nothing was submitted"
                    )
                    await asyncio.to_thread(self.dao.fail, run["id"], msg)
                    log.error("dvc: %s run %s %s", run["job"], run["id"], msg)
                    await self._emit(
                        "error",
                        job=run["job"],
                        run_id=run["id"],
                        trigger=run["trigger"],
                        started_at=run["started_at"],
                        message=msg,
                    )
                continue
            log.info(
                "dvc: adopting live run %s (%s, task %s)",
                run["id"], run["job"], run["task_id"],
            )
            self._start_watch(run)

    async def _watch(self, run: dict) -> None:
        """Poll one Globus task to a verdict, recording before every emit."""
        run_id, job, task_id = run["id"], run["job"], run["task_id"]
        started = float(run.get("started_at") or 0)
        trigger = str(run.get("trigger") or "")
        alerted = bool(run.get("alerted_at"))
        last_key: tuple | None = None
        last_emit = 0.0
        failures = 0

        while True:
            try:
                cfg = await asyncio.to_thread(load_config)
                doc = await asyncio.to_thread(globus.task_status, cfg, task_id)
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.warning(
                    "dvc: poll %d/%d failed for task %s: %s",
                    failures, _MAX_POLL_FAILURES, task_id, exc,
                )
                if failures >= _MAX_POLL_FAILURES:
                    msg = f"gave up polling task {task_id}: {exc}"
                    await asyncio.to_thread(self.dao.fail, run_id, msg)
                    await self._emit(
                        "error", job=job, run_id=run_id, trigger=trigger,
                        task_id=task_id, started_at=started, message=msg,
                    )
                    return
                await asyncio.sleep(WATCH_POLL_S)
                continue

            status = await asyncio.to_thread(
                jobs.record_status, self.dao, run_id, doc
            )
            if status in globus.TERMINAL:
                ok = status == "SUCCEEDED"
                log.log(
                    logging.INFO if ok else logging.ERROR,
                    "dvc: %s task %s %s (files=%s/%s bytes=%s faults=%s) %s",
                    job, task_id, status, doc.get("files_transferred"),
                    doc.get("files"), doc.get("bytes_transferred"),
                    doc.get("faults"), doc.get("nice_status") or "",
                )
                await self._emit(
                    "succeeded" if ok else "failed",
                    job=job, run_id=run_id, trigger=trigger, task_id=task_id,
                    started_at=started, doc=doc,
                    message=str(doc.get("nice_status") or ""),
                )
                return

            now = time()
            if not alerted and started and now - started > STALL_AFTER_S:
                # Not a status: only Globus decides a verdict. This is the one
                # ERROR line anyone will ever be paged by, so it says the number.
                alerted = True
                await asyncio.to_thread(self.dao.mark_alerted, run_id)
                hours = (now - started) / 3600
                log.error(
                    "dvc: %s task %s still running after %.1fh — still polling",
                    job, task_id, hours,
                )
                await self._emit(
                    "stalled", job=job, run_id=run_id, trigger=trigger,
                    task_id=task_id, started_at=started, doc=doc,
                    message=f"still running after {hours:.1f}h",
                )

            key = (
                doc.get("status"), doc.get("files_transferred"),
                doc.get("bytes_transferred"), doc.get("faults"),
            )
            if key != last_key or now - last_emit >= PROGRESS_EMIT_S:
                last_key, last_emit = key, now
                await self._emit(
                    "progress", job=job, run_id=run_id, trigger=trigger,
                    task_id=task_id, started_at=started, doc=doc,
                )
            await asyncio.sleep(WATCH_POLL_S)

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        log.info("dvc: scheduler loop started")
        await asyncio.to_thread(jobs.ensure_seeded, self.dao)
        while True:
            now = time()
            try:
                due = await asyncio.to_thread(self._reconcile, now)
            except Exception as exc:  # noqa: BLE001 — a DB hiccup skips a tick
                log.warning("dvc: reconcile failed: %s", exc)
                due = []
            for name, trigger in due:
                asyncio.create_task(self._fire(name, trigger), name=f"dvc-fire:{name}")
            try:
                await self._adopt_sweep()
            except Exception as exc:  # noqa: BLE001
                log.warning("dvc: adopt sweep failed: %s", exc)
            self._ticks += 1
            self._last_tick = time()
            await asyncio.sleep(self._sleep_for(time()))
