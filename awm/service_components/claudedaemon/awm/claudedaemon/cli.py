"""Ending a background session through the `claude` CLI.

Decides nothing about whether the caller may end it; that stays with the
service that asks.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from awm.claudedaemon import launch, roster
from awm.claudedaemon.sessionmode import JOB_PATTERN

log = logging.getLogger("awm.claudedaemon.cli")

TIMEOUT_S = 20.0
WORKER_EXIT_S = 10.0
SETTLE_S = 3.0
RM_ATTEMPTS = 3
POLL_S = 0.25


def _run(claude: str, verb: str, job: str, timeout: float,
         run: Callable[..., subprocess.CompletedProcess]) -> tuple[bool, str]:
    try:
        proc = run([claude, verb, job], stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"claude {verb} timed out"
    except OSError as exc:
        return False, f"claude {verb} could not run: {exc}"
    out = (proc.stdout or b"")
    text = out.decode(errors="replace") if isinstance(out, bytes) else str(out)
    return proc.returncode == 0, text.strip()[:300]


@dataclass(frozen=True)
class JobProbe:
    """Where a job still shows: its worker process, and its record or roster entry."""

    roster_path: Path | None = None
    jobs_dir: Path | None = None

    def _worker(self, job: str) -> dict:
        workers = roster.read_json(self.roster_path or roster.default_roster_path()).get("workers")
        w = workers.get(job) if isinstance(workers, dict) else None
        return w if isinstance(w, dict) else {}

    def worker_alive(self, job: str) -> bool:
        w = self._worker(job)
        pid, start = w.get("pid"), w.get("procStart")
        return isinstance(pid, int) and start is not None and roster.proc_start(pid) == str(start)

    def present(self, job: str) -> bool:
        jobs = self.jobs_dir or roster.default_jobs_dir()
        return (jobs / job).exists() or bool(self._worker(job))


def stop_and_remove(job: str, *, claude: str | None = None,
                    timeout: float = TIMEOUT_S,
                    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                    probe: JobProbe | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> dict:
    """`claude stop <job>`, then `claude rm <job>` until the job is gone.

    `claude stop` returns before the worker has exited, and the daemon writes
    the job's record once more when it settles the exit. An `rm` that runs in
    between deletes the record, which then comes back, and the job sits in the
    fleet view as stopped. So the removal waits for the worker, and success is
    the job being absent afterwards, not `rm`'s exit code. A job that is
    already stopped makes the stop fail and the removal still proceed.
    """
    if not isinstance(job, str) or not JOB_PATTERN.match(job):
        raise ValueError("job must be a job id")
    claude = claude or launch.default_claude_bin()
    probe = probe or JobProbe()
    stopped, stop_detail = _run(claude, "stop", job, timeout, run)
    _wait(lambda: not probe.worker_alive(job), WORKER_EXIT_S, sleep, clock)
    rm_detail = ""
    removed = False
    for _ in range(RM_ATTEMPTS):
        _, rm_detail = _run(claude, "rm", job, timeout, run)
        removed = _wait(lambda: not probe.present(job), SETTLE_S, sleep, clock)
        if removed:
            break
    if not removed:
        log.warning("claudedaemon: removing %s failed: %s", job, rm_detail or "the job is still there")
    return {"stopped": stopped, "removed": removed,
            "detail": (rm_detail or "the job is still there") if not removed else stop_detail}


def _wait(done: Callable[[], bool], seconds: float, sleep: Callable[[float], None],
          clock: Callable[[], float]) -> bool:
    deadline = clock() + seconds
    while not done():
        if clock() >= deadline:
            return False
        sleep(POLL_S)
    return True
