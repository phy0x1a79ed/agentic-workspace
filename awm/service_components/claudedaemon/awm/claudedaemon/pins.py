"""Pinned background jobs, which the daemon retires last.

``~/.claude/jobs/pins.json`` is a JSON array of job ids. The Claude CLI guards
it with a ``pins.json.lock`` directory that it treats as stale after five
seconds; this takes the same lock.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Iterable, Iterator

from awm.claudedaemon import sessionmode

STALE_S = 5.0


def pins_path() -> Path:
    return sessionmode.jobs_dir() / "pins.json"


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


def _read(path: Path) -> list[str]:
    try:
        current = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(current, list):
        raise ValueError(f"{path} is not a JSON array; leaving it alone")
    return current


def _write(path: Path, jobs: list[str]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(jobs))
    tmp.replace(path)


def pinned(path: Path | None = None) -> list[str]:
    try:
        return [j for j in _read(path or pins_path()) if isinstance(j, str)]
    except (OSError, ValueError):
        return []


def pin(job: str, path: Path | None = None) -> None:
    path = path or pins_path()
    with _locked(path):
        current = _read(path)
        if job not in current:
            _write(path, [*current, job])


def unpin(jobs: str | Iterable[str], path: Path | None = None) -> list[str]:
    """Drop jobs from the pin list; returns the ones that were pinned."""
    drop = {jobs} if isinstance(jobs, str) else set(jobs)
    path = path or pins_path()
    with _locked(path):
        current = _read(path)
        dropped = [j for j in current if j in drop]
        if dropped:
            _write(path, [j for j in current if j not in drop])
    return dropped
