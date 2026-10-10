"""Keeping one representative and one secretary alive.

Modelled on the cx reconcile loop: sleep, tick, never die on an exception, and
hold a non-blocking flock for the life of the process so a second copy of the
service (an overlay, a dev sandbox, a stray manual run) cannot start a second
pair of sessions against the same node.

A tick asks cx which sessions exist. When it cannot get an answer it does
nothing, because starting on an unknown state is how duplicates happen. A role
is present when a live session carries its mode label *and* is one the door
started (the queue database records the job ids). Other sessions carrying the
label are logged and ignored. The door starts a missing role with the launch
config from `awm.representative.personas` and never takes a session.

A present session is alive only when it can work (`sessions.assess`). What the
door does with the others:

* running: nothing.
* exited or missing: start one, backing off after a failed start.
* waiting (a rate limit, overload, a question the session asked): nothing. It
  clears on the next prompt.
* blocked (login expired, usage limit, account or org problem): nothing but a
  warning when the state changes. A restart cannot answer a login, and a loop
  of restarts would only pile up sessions, so the door leaves the session for a
  person. When it recovers the door calls `on_started`, so queued cards are
  announced again at once.
* failed (an unclassified API error): stop it, which the door may do because it
  started it, and start a new one. Restarts back off, doubling from
  `MIN_RETRY_S`. A session's own "failed: ..." line is not this state.
"""

from __future__ import annotations

import asyncio
import fcntl
import importlib
import logging
import os
import time
from typing import Any, Callable

from awm import gatewayclient

from awm.representative import config, sessions
from awm.representative.store import Queue

log = logging.getLogger("awm.representative.reconcile")

ROLES = ("representative", "secretary")

#: How long the same note is swallowed before it is logged again.
QUIET_S = 600.0
#: The shortest wait before a failed start is tried again.
MIN_RETRY_S = 60.0
MAX_RETRY_S = 900.0


class Loop:
    def __init__(self, queue: Queue, cx: Any = None, personas: Any = None,
                 interval_s: float | None = None,
                 on_started: Callable[[str], None] | None = None,
                 scope_call: Callable[..., Any] | None = None) -> None:
        self.queue = queue
        self.cx = cx or sessions.Cx()
        self._on_started = on_started or (lambda role: None)
        self._scope_call = scope_call or gatewayclient.call
        self._personas = personas
        self._interval_s = interval_s
        self._lock_fd: int | None = None
        self._last_tick: float | None = None
        self._notes: dict[str, float] = {}
        self._next_try: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._restarts: dict[str, int] = {}
        self._restarted_at: dict[str, float] = {}
        self._seen: dict[str, tuple] = {}
        self.jobs: dict[str, str] = {}
        self._running = False

    # -- what the adapter calls ----------------------------------------------

    async def run(self) -> None:
        """Tick, then sleep, forever. Never returns, never raises."""
        self._running = True
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a tick must never end the loop
                log.exception("door: reconcile tick failed")
            await asyncio.sleep(self._interval_s or config.reconcile_interval_s())

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "holds_lock": self._lock_fd is not None,
            "last_tick_s_ago": (None if self._last_tick is None
                                else round(time.time() - self._last_tick, 1)),
            "jobs": dict(self.jobs),
        }

    async def representative_job(self) -> str | None:
        """The live representative's job id, read fresh from cx. None when absent or unknown."""
        spec = self._spec("representative")
        if spec is None:
            return None
        try:
            return sessions.find_job(await self.cx.list(), spec["mode"],
                                     self.queue.session_jobs("representative"))
        except sessions.CxUnavailable:
            return None

    async def health(self) -> dict[str, dict[str, Any]]:
        """Per role: ``alive`` (None when cx could not say), ``state``, ``reason`` and ``job``."""
        try:
            rows = await self.cx.list()
        except sessions.CxUnavailable as exc:
            unknown = {"alive": None, "state": "unknown", "reason": str(exc), "job": None}
            return {role: dict(unknown) for role in ROLES}
        out: dict[str, dict[str, Any]] = {}
        for role in ROLES:
            spec = self._spec(role)
            if spec is None:
                out[role] = {"alive": None, "state": "unknown", "reason": "no launch config", "job": None}
                continue
            out[role] = sessions.assess(rows, spec["mode"], self.queue.session_jobs(role)).as_dict()
        return out

    async def alive(self) -> dict[str, bool | None]:
        """Whether each role has a session that can work. None means cx could not say."""
        return {role: h["alive"] for role, h in (await self.health()).items()}

    # -- one tick ------------------------------------------------------------

    async def tick(self) -> None:
        why = config.refusal()
        if why is not None:
            self._note("refused", f"not reconciling: {why}")
            return
        if not self._take_lock():
            return
        await self._ensure_work_scope()
        try:
            rows = await self.cx.list()
        except sessions.CxUnavailable as exc:
            self._note("cx", f"not reconciling: {exc}")
            return
        for role in ROLES:
            await self._ensure(role, rows)
        self._last_tick = time.time()

    async def _ensure_work_scope(self) -> None:
        """Make sure the scope delegates start in exists. A failure is logged and retried next tick.

        `scope_create` is the only call that makes the worktree directory cx checks
        for, so the check is the directory itself and nothing is called when it is there.
        """
        where = getattr(self._personas_obj(), "work_where", None)
        if where is None:
            return
        project, scope = where()
        if (config.projects_dir() / project / scope).is_dir():
            return
        try:
            await self._scope_call("scopes", "scope_create",
                                   {"project": project, "scope": scope}, timeout=300.0)
        except Exception as exc:  # noqa: BLE001 — the roles still need their sessions
            self._note("workscope", f"could not create the work scope {project}/{scope}: {exc}")
            return
        if (config.projects_dir() / project / scope).is_dir():
            log.info("door: created the work scope %s/%s", project, scope)
        else:
            self._note("workscope", f"scope_create answered but {project}/{scope} has no worktree")

    async def _ensure(self, role: str, rows: list[dict]) -> None:
        spec = self._spec(role)
        if spec is None:
            return
        problem = self._problem(spec)
        if problem:
            self._note(f"spec:{role}", f"{role}: {problem}")
            return
        jobs = self.queue.session_jobs(role)
        adopted = self._adopt(role, spec, rows, jobs)
        if adopted:
            jobs = self.queue.session_jobs(role)
        for stranger in sessions.strangers(rows, spec["mode"], jobs):
            self._note(f"stranger:{stranger.get('job')}",
                       f"{role}: session {stranger.get('job')} carries mode {spec['mode']!r} "
                       "but the door did not start it; ignoring it")
        have = sessions.holders(rows, spec["mode"], jobs)
        health = sessions.assess(rows, spec["mode"], jobs)
        self._report(role, health)
        stopped: str | None = None
        if have:
            if len(have) > 1:
                self._note(f"dup:{role}", f"{role}: {len(have)} live sessions carry mode "
                           f"{spec['mode']!r}; the door leaves them alone")
            if not health.restartable:
                self.jobs[role] = health.job or ""
                if health.alive:
                    self._failures.pop(role, None)
                    self._settle_restarts(role)
                return
            stopped = await self._stop_failed(role, health)
            if stopped is None:
                return
        self.jobs.pop(role, None)
        taken = [r for r in rows if r.get("name") == spec.get("name")
                 and r.get("state") != sessions.GONE and r.get("job") != stopped]
        if taken:
            self._note(f"name:{role}", f"{role}: a session the door did not start already "
                       f"holds the name {spec.get('name')!r}; not starting")
            return
        if stopped is None and self._next_try.get(role, 0.0) > time.time():
            return
        reply = await self.cx.start(spec)
        if reply.get("ok"):
            self._failures.pop(role, None)
            if stopped is None:
                self._next_try.pop(role, None)
            self.jobs[role] = reply.get("job") or ""
            if reply.get("job"):
                self.queue.record_session(role, reply["job"])
            log.info("door: started the %s as %s", role, reply.get("job"))
            self._on_started(role)
            return
        failures = self._failures[role] = self._failures.get(role, 0) + 1
        wait = min(MAX_RETRY_S, max(MIN_RETRY_S, (self._interval_s or config.reconcile_interval_s()))
                   * 2 ** (failures - 1))
        self._next_try[role] = time.time() + wait
        self._note(f"start:{role}", f"{role}: cx would not start it: "
                   f"{reply.get('reason') or reply.get('error') or reply!r}")

    async def _stop_failed(self, role: str, health: sessions.Health) -> str | None:
        """Stop the door's failed session so a new one can start. Returns its job, or None to wait.

        A failed session is stopped at most once per back-off period, so a
        session that fails as soon as it starts cannot make the door churn.
        """
        if self._next_try.get(role, 0.0) > time.time():
            return None
        self._restarts[role] = self._restarts.get(role, 0) + 1
        self._restarted_at[role] = time.time()
        self._next_try[role] = time.time() + self._restart_wait(role)
        reply = await self.cx.stop(health.job or "")
        if not reply.get("ok"):
            self._note(f"stop:{role}", f"{role}: cx would not stop the failed session "
                       f"{health.job}: {reply.get('reason') or reply!r}")
            return None
        log.info("door: stopped the failed %s %s (%s); starting a new one", role, health.job, health.reason)
        return health.job

    def _restart_wait(self, role: str) -> float:
        base = max(MIN_RETRY_S, self._interval_s or config.reconcile_interval_s())
        return min(MAX_RETRY_S, base * 2 ** (self._restarts.get(role, 1) - 1))

    def _settle_restarts(self, role: str) -> None:
        """Forget past restarts once the replacement has run for a while without failing."""
        since = self._restarted_at.get(role)
        if since is not None and time.time() - since >= QUIET_S:
            self._restarts.pop(role, None)
            self._restarted_at.pop(role, None)

    def _report(self, role: str, health: sessions.Health) -> None:
        """Log a session that cannot work once per change of state, and wake the role on recovery."""
        seen = (health.job, health.state, health.reason)
        before = self._seen.get(role)
        if before == seen:
            return
        self._seen[role] = seen
        if health.state in (sessions.BLOCKED, sessions.FAILED):
            log.warning("door: the %s (%s) is %s: %s", role, health.job, health.state,
                        health.reason or "no reason given")
        elif health.alive and before is not None and before[1] in (sessions.BLOCKED, sessions.FAILED):
            log.info("door: the %s (%s) can work again", role, health.job)
            self._on_started(role)

    def _adopt(self, role: str, spec: dict, rows: list[dict], jobs: set[str]) -> bool:
        """Record a session of this role that the door started but never recorded.

        A `cx start` that timed out or failed after the job launched leaves a
        live session the door has no record of. It is the door's own when it
        carries the role's reserved name and mode label and its lineage shows a
        caller with no session pid: only the door starts those, and cx refuses
        everyone else. Returns True if a job was recorded.
        """
        found = False
        for row in sessions.strangers(rows, spec["mode"], jobs):
            if (row.get("name") == spec.get("name") and row.get("job")
                    and row.get("caller") is not None and row.get("parent") is None):
                self.queue.record_session(role, row["job"])
                log.info("door: adopted %s as the %s; its start outlived the call", row["job"], role)
                found = True
        if found:
            self._on_started(role)
        return found

    @staticmethod
    def _problem(spec: dict) -> str | None:
        if not spec.get("mode"):
            return "the launch config has no mode, so the door cannot tell its session apart"
        if not spec.get("permission"):
            return "the launch config has no permission; cx would default to skip-permissions"
        return None

    def _personas_obj(self) -> Any:
        if self._personas is not None:
            return self._personas
        try:
            return importlib.import_module("awm.representative.personas")
        except ImportError as exc:
            self._note("personas", f"no launch configs: {exc}")
            return None

    def _spec(self, role: str) -> dict | None:
        personas = self._personas_obj()
        if personas is None:
            return None
        spec = getattr(personas, role.upper(), None)
        if not isinstance(spec, dict):
            self._note(f"personas:{role}", f"personas has no {role.upper()} launch config")
            return None
        return spec

    def _note(self, key: str, message: str) -> None:
        last = self._notes.get(key)
        if last is not None and time.time() - last < QUIET_S:
            return
        log.info("door: %s", message)
        self._notes[key] = time.time()

    # -- mutual exclusion ----------------------------------------------------

    def _take_lock(self) -> bool:
        """Hold the reconcile lock, taking it if this process does not have it.

        Held for the life of the process. The kernel drops it when the process
        dies, so a killed copy hands over with no cleanup pass.
        """
        if self._lock_fd is not None:
            return True
        path = config.state_dir() / "reconcile.lock"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            self._note("lock", f"cannot open the reconcile lock: {exc}")
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            self._note("lock", "another door holds the reconcile lock; not reconciling")
            return False
        self._lock_fd = fd
        log.info("door: holding the reconcile lock")
        return True
