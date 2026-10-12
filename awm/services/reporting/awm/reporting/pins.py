"""Pinning the persona sessions so the daemon keeps them, and removing the ones they replace.

``~/.claude/jobs/pins.json`` is a JSON array of job ids. The Claude CLI guards
it with a ``pins.json.lock`` directory that it treats as stale after five
seconds; this takes the same lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterator

from awm.claudedaemon import launch, roster

log = logging.getLogger("awm.reporting.pins")

STALE_S = 5.0
RM_TIMEOUT_S = 60.0


def pins_path() -> Path:
    return roster.claude_home() / "jobs" / "pins.json"


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = Path(f"{path}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > STALE_S:
                    lock.rmdir()
                    continue
            time.sleep(0.02)
    else:
        raise TimeoutError(f"{lock} stayed locked")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.rmdir()


def _edit(job: str, pinned: bool, path: Path | None = None) -> None:
    path = path or pins_path()
    with _locked(path):
        try:
            current = json.loads(path.read_text())
        except FileNotFoundError:
            current = []
        if not isinstance(current, list):
            raise ValueError(f"{path} is not a JSON array; leaving it alone")
        wanted = [j for j in current if j != job] if not pinned else [*current, job]
        if (job in current) != pinned:
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(wanted))
            tmp.replace(path)


class Pins:
    async def pin(self, job: str) -> None:
        await self._try(asyncio.to_thread(_edit, job, True), f"pin {job}")

    async def release(self, job: str) -> None:
        """Unpin a job and `claude rm` it, so a replaced session leaves nothing behind."""
        await self._try(asyncio.to_thread(_edit, job, False), f"unpin {job}")
        await self._try(self._rm(job), f"claude rm {job}")

    async def _rm(self, job: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            launch.default_claude_bin(), "rm", job,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=RM_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            raise
        if proc.returncode:
            raise RuntimeError((out or b"").decode(errors="replace").strip()[:300])

    @staticmethod
    async def _try(awaitable, what: str) -> None:
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001 — a persona must never wait on housekeeping
            log.warning("reporting: could not %s: %s", what, exc)
