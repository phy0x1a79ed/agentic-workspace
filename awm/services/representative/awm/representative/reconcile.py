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
config from `awm.representative.personas` and never stops or takes a session.
"""

from __future__ import annotations

import asyncio
import fcntl
import importlib
import logging
import os
import time
from typing import Any, Callable

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
                 on_started: Callable[[str], None] | None = None) -> None:
        self.queue = queue
        self.cx = cx or sessions.Cx()
        self._on_started = on_started or (lambda role: None)
        self._personas = personas
        self._interval_s = interval_s
        self._lock_fd: int | None = None
        self._last_tick: float | None = None
        self._notes: dict[str, float] = {}
        self._next_try: dict[str, float] = {}
        self._failures: dict[str, int] = {}
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

    async def alive(self) -> dict[str, bool | None]:
        """Whether each role has a live session. None means cx could not say."""
        try:
            rows = await self.cx.list()
        except sessions.CxUnavailable:
            return {role: None for role in ROLES}
        out: dict[str, bool | None] = {}
        for role in ROLES:
            spec = self._spec(role)
            out[role] = (bool(sessions.holders(rows, spec["mode"], self.queue.session_jobs(role)))
                         if spec else None)
        return out

    # -- one tick ------------------------------------------------------------

    async def tick(self) -> None:
        why = config.refusal()
        if why is not None:
            self._note("refused", f"not reconciling: {why}")
            return
        if not self._take_lock():
            return
        try:
            rows = await self.cx.list()
        except sessions.CxUnavailable as exc:
            self._note("cx", f"not reconciling: {exc}")
            return
        for role in ROLES:
            await self._ensure(role, rows)
        self._last_tick = time.time()

    async def _ensure(self, role: str, rows: list[dict]) -> None:
        spec = self._spec(role)
        if spec is None:
            return
        problem = self._problem(spec)
        if problem:
            self._note(f"spec:{role}", f"{role}: {problem}")
            return
        jobs = self.queue.session_jobs(role)
        for stranger in sessions.strangers(rows, spec["mode"], jobs):
            self._note(f"stranger:{stranger.get('job')}",
                       f"{role}: session {stranger.get('job')} carries mode {spec['mode']!r} "
                       "but the door did not start it; ignoring it")
        have = sessions.holders(rows, spec["mode"], jobs)
        if have:
            self.jobs[role] = have[0].get("job") or ""
            self._failures.pop(role, None)
            if len(have) > 1:
                self._note(f"dup:{role}", f"{role}: {len(have)} live sessions carry mode "
                           f"{spec['mode']!r}; the door leaves them alone")
            return
        self.jobs.pop(role, None)
        taken = [r for r in rows if r.get("name") == spec.get("name") and r.get("state") != sessions.GONE]
        if taken:
            self._note(f"name:{role}", f"{role}: a session the door did not start already "
                       f"holds the name {spec.get('name')!r}; not starting")
            return
        if self._next_try.get(role, 0.0) > time.time():
            return
        reply = await self.cx.start(spec)
        if reply.get("ok"):
            self._failures.pop(role, None)
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

    @staticmethod
    def _problem(spec: dict) -> str | None:
        if not spec.get("mode"):
            return "the launch config has no mode, so the door cannot tell its session apart"
        if not spec.get("permission"):
            return "the launch config has no permission; cx would default to skip-permissions"
        return None

    def _spec(self, role: str) -> dict | None:
        personas = self._personas
        if personas is None:
            try:
                personas = importlib.import_module("awm.representative.personas")
            except ImportError as exc:
                self._note("personas", f"no launch configs: {exc}")
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
