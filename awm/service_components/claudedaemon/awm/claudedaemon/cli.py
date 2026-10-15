"""Ending a background session through the `claude` CLI.

Decides nothing about whether the caller may end it; that stays with the
service that asks.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable

from awm.claudedaemon import launch
from awm.claudedaemon.sessionmode import JOB_PATTERN

log = logging.getLogger("awm.claudedaemon.cli")

TIMEOUT_S = 20.0


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


def stop_and_remove(job: str, *, claude: str | None = None,
                    timeout: float = TIMEOUT_S,
                    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                    ) -> dict:
    """`claude stop <job>` then `claude rm <job>`.

    A job that is already stopped makes the stop fail and the removal still
    proceed, so success is the removal's: ``removed``. ``stopped`` and
    ``detail`` say what each step answered.
    """
    if not isinstance(job, str) or not JOB_PATTERN.match(job):
        raise ValueError("job must be a job id")
    claude = claude or launch.default_claude_bin()
    stopped, stop_detail = _run(claude, "stop", job, timeout, run)
    removed, rm_detail = _run(claude, "rm", job, timeout, run)
    if not removed:
        log.warning("claudedaemon: removing %s failed: %s", job, rm_detail)
    return {"stopped": stopped, "removed": removed,
            "detail": rm_detail if not removed else stop_detail}
