"""Shared git helpers — run_git() and detect_default_branch()."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, capturing output as text."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def detect_default_branch(bare_dir: Path) -> str:
    """Detect the default branch in a bare repo.

    Strategy:
    1. symbolic-ref HEAD — most authoritative
    2. rev-parse --verify main / master
    3. refs/remotes/origin/HEAD
    4. Fallback: "main"
    """
    # 1. symbolic-ref HEAD
    r = run_git(["git", "-C", str(bare_dir), "symbolic-ref", "HEAD"])
    if r.returncode == 0:
        ref = r.stdout.strip()  # e.g. refs/heads/release
        if ref.startswith("refs/heads/"):
            return ref.removeprefix("refs/heads/")

    # 2. rev-parse --verify for common names
    for branch in ("main", "master"):
        r = run_git(["git", "-C", str(bare_dir), "rev-parse", "--verify", branch])
        if r.returncode == 0:
            return branch

    # 3. refs/remotes/origin/HEAD
    r = run_git(["git", "-C", str(bare_dir), "symbolic-ref", "refs/remotes/origin/HEAD"])
    if r.returncode == 0:
        ref = r.stdout.strip()  # e.g. refs/remotes/origin/release
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]

    # 4. Fallback
    return "main"
