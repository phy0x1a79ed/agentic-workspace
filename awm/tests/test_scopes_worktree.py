"""Real-git regression test for _cleanup_worktree sibling-integrity (inbox #111)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awm.services.scopes import _cleanup_worktree


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def bare_with_two_worktrees(tmp_path: Path):
    """Create a bare repo with an initial commit and two worktrees `a` and `b`."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("x")
    _git(seed, "add", "README")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    bare = tmp_path / ".bare"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )

    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    _git(bare, "worktree", "add", "-b", "feat/a", str(a_dir), "main")
    _git(bare, "worktree", "add", "-b", "feat/b", str(b_dir), "main")
    return bare, a_dir, b_dir


def test_cleanup_does_not_wipe_sibling_worktree(bare_with_two_worktrees):
    bare, a_dir, b_dir = bare_with_two_worktrees

    # Sanity: both metadata dirs exist before cleanup.
    assert (bare / "worktrees" / "a").exists()
    assert (bare / "worktrees" / "b").exists()

    _cleanup_worktree(bare, a_dir, "feat/a")

    # Target gone.
    assert not a_dir.exists()
    assert not (bare / "worktrees" / "a").exists()

    # Sibling fully intact: directory, gitlink, metadata, and git commands work.
    assert b_dir.exists()
    assert (b_dir / ".git").exists()
    assert (bare / "worktrees" / "b").exists()
    r = subprocess.run(
        ["git", "-C", str(b_dir), "status"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"sibling worktree broken: {r.stderr}"
