"""Commit a service's writes into a user's worktree, and pin figures with DVC.

A per-user store (notes, drawio, …) lives as a subdirectory of the user's
scope worktree. After a flush the service calls :func:`commit_subdir` for its
own subdirectory only — never ``git add -A`` at the root, which would sweep
another service's half-written files into this commit. The commit carries the
same ``Author-Handle:`` trailer the drawio store uses, so history stays
attributable after the user branches are merged.

:func:`pin_chunk` is the DVC half: when a chunk changed since its ``.dvc``
pin, re-add and commit the pin. Nothing pushes; the cache is local.
:func:`pin_figures` is that call for the per-user figures directory, which was
the only chunk any service pinned until the vault started holding a mirrored
Zotero library.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("awm.config.autocommit")

GIT_IDENTITY = ("-c", "user.name=awm", "-c", "user.email=awm@localhost")
DVC_ENV = "AWM_DVC_BIN"
FIGURES = Path("data") / "figures"
# Same known locations scopes' data_dvc looks in: a service under systemd
# has a minimal PATH and dvc may live in its own env.
DVC_FALLBACKS = (
    Path.home() / "lib/miniforge3/envs/dvc/bin/dvc",
    Path.home() / "lib/miniforge3/envs/awm/bin/dvc",
    Path("/opt/miniforge3/envs/dvc/bin/dvc"),
)

# A chunk dvc refuses outright — one git already tracks, say — fails identically
# on every tick, so back a failure off instead of retrying it every time. Two
# things end the backoff: the chunk changes, or the hour is up. The hour matters
# because the cause often sits outside the chunk (repo state, a missing cache),
# so a fix applied elsewhere must heal without waiting for a restart.
_RETRY_AFTER_S = 3600.0
_failed: dict[tuple[str, str], tuple[float, float]] = {}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(root), *args],
        capture_output=True, text=True, check=False,
    )


def _commit(root: Path, paths: list[str], author: str, message: str) -> str | None:
    r = _git(root, "commit", "--quiet", "--no-verify", "-m", message,
             "-m", f"Author-Handle: user:{author}", "--", *paths)
    if r.returncode != 0:
        log.warning("autocommit: commit in %s failed: %s", root, r.stderr.strip())
        return None
    return _git(root, "rev-parse", "HEAD").stdout.strip() or None


def commit_subdir(root: Path, subdir: str, author: str, message: str) -> str | None:
    """Stage and commit every change under ``root/subdir``. Returns the new
    commit sha, or ``None`` when there was nothing to commit."""
    root = Path(root)
    if _git(root, "add", "-A", "--", subdir).returncode != 0:
        log.warning("autocommit: git add failed in %s/%s", root, subdir)
        return None
    if _git(root, "diff", "--cached", "--quiet", "--", subdir).returncode == 0:
        return None
    return _commit(root, [subdir], author, message)


def dvc_bin() -> str | None:
    override = os.environ.get(DVC_ENV)
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("dvc")
    if found:
        return found
    for cand in DVC_FALLBACKS:
        if os.access(cand, os.X_OK):
            return str(cand)
    return None


def _newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for p in path.rglob("*"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def pin_chunk(root: Path, chunk: str, author: str,
              message: str | None = None) -> str | None:
    """``dvc add <chunk>`` and commit the pin when the chunk moved.

    Silent no-op unless the worktree is DVC-initialised (``.dvc/config``) and
    the chunk exists. Returns the commit sha, or ``None``.

    The mtime comparison against the pin is the cheap half: a pass that finds
    nothing newer than the ``.dvc`` file does not shell out to dvc at all,
    which is what lets a scheduled sync call this every tick.

    ``dvc add`` writes two things — the pin, and a ``.gitignore`` beside the
    chunk telling git to leave the bytes alone. Committing the first without
    the second stages the bytes as blobs on somebody's next commit, so both
    are always staged together.

    A failure backs off until the chunk changes or an hour passes, so a chunk
    dvc will never accept costs one subprocess an hour rather than one a tick.
    """
    root, rel = Path(root), Path(chunk)
    target = root / rel
    if not (root / ".dvc" / "config").is_file() or not target.is_dir():
        return None
    pin = target.with_suffix(target.suffix + ".dvc")
    newest = _newest_mtime(target)
    if pin.is_file() and newest <= pin.stat().st_mtime:
        return None
    key = (str(root), chunk)
    prev = _failed.get(key)
    if prev and prev[0] == newest and time.monotonic() - prev[1] < _RETRY_AFTER_S:
        return None
    dvc = dvc_bin()
    if not dvc:
        log.warning("autocommit: %s changed in %s but dvc is not installed",
                    chunk, root)
        return None
    # No --quiet: it suppresses the ERROR text along with the chatter, and that
    # text is the entire content of the warning below. Output is captured either
    # way, so dvc sees a pipe rather than a tty and draws no progress bar.
    # DVC_NO_ANALYTICS: a scheduled loop has no business phoning home, and a run
    # that fails strands its unsent report in the system temp dir for good.
    r = subprocess.run([dvc, "add", str(rel)], cwd=root,
                       capture_output=True, text=True, check=False,
                       env={**os.environ, "DVC_NO_ANALYTICS": "1"})
    if r.returncode != 0:
        _failed[key] = (newest, time.monotonic())
        log.warning("autocommit: dvc add %s failed in %s (rc=%d): %s", chunk,
                    root, r.returncode, r.stderr.strip() or r.stdout.strip())
        return None
    _failed.pop(key, None)
    if not pin.is_file():
        return None
    os.utime(pin, None)
    paths = [str(pin.relative_to(root)), str((rel.parent / ".gitignore"))]
    _git(root, "add", "--", *[p for p in paths if (root / p).exists()])
    if _git(root, "diff", "--cached", "--quiet", "--", *paths).returncode == 0:
        return None
    return _commit(root, paths, author, message or f"pin {chunk}")


def pin_figures(root: Path, author: str) -> str | None:
    """Pin the per-user figures directory. The one caller this had before
    :func:`pin_chunk` existed, kept because its commit message is part of a
    history people read."""
    return pin_chunk(root, str(FIGURES), author, "figures: pin data/figures")
