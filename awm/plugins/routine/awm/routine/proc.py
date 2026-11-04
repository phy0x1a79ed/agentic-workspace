"""Run one shell command in its own process group, with a time budget.

A timeout or a cancel kills the whole group, so a step's children die with it.
:func:`git` is the blocking git call the deploy, refresh and runner modules share.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass

TAIL = 4000
KILL_GRACE = 5.0


def git(root: Path | str, *args: str) -> str:
    """``git -C root args``, stripped stdout. A failure raises CalledProcessError."""
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


@dataclass
class Result:
    code: int | None
    output: str
    timed_out: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def _kill_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    _kill_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE)
    except asyncio.TimeoutError:
        _kill_group(proc, signal.SIGKILL)
        await proc.wait()


async def run(cmd: str, *, cwd: str, timeout: float,
              env: dict[str, str] | None = None) -> Result:
    """Run ``cmd`` under bash. Cancelling the awaiting task kills the group."""
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", cmd, cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    chunks: list[bytes] = []

    async def pump() -> None:
        assert proc.stdout is not None
        size = 0
        while block := await proc.stdout.read(65536):
            chunks.append(block)
            size += len(block)
            while size > 4 * TAIL and len(chunks) > 1:
                size -= len(chunks.pop(0))

    reader = asyncio.create_task(pump())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        await _reap(proc)
    except asyncio.CancelledError:
        await asyncio.shield(_reap(proc))
        reader.cancel()
        raise
    try:
        await asyncio.wait_for(reader, 2.0)
    except asyncio.TimeoutError:
        reader.cancel()
    out = b"".join(chunks).decode(errors="replace")[-TAIL:]
    return Result(code=proc.returncode, output=out, timed_out=timed_out,
                  seconds=round(time.monotonic() - t0, 3))
