"""Real-git regression tests for _cleanup_worktree sibling-integrity (inbox #111)
and for the basename collision nested scope names introduce."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

import subprocess
from pathlib import Path

import pytest

from awm.scopes.scopes import _cleanup_worktree, _worktree_admin_dir


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


@pytest.fixture
def bare_with_colliding_basenames(tmp_path: Path):
    """A flat scope ``dev`` and a nested scope ``fabfos/dev`` in one project.

    Their worktrees share a basename, so git disambiguates the admin dirs
    (``worktrees/dev`` and ``worktrees/dev1``). Which one belongs to which is
    exactly what a basename-derived guess gets wrong.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("x")
    _git(seed, "add", "README")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    bare = tmp_path / ".bare"
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(bare)],
                   check=True, capture_output=True)

    flat = tmp_path / "dev"
    nested = tmp_path / "fabfos" / "dev"
    _git(bare, "worktree", "add", "-b", "feat/dev", str(flat), "main")
    _git(bare, "worktree", "add", "-b", "fabfos/dev", str(nested), "main")
    return bare, flat, nested


def test_admin_dirs_are_distinct_despite_shared_basename(bare_with_colliding_basenames):
    bare, flat, nested = bare_with_colliding_basenames
    a = _worktree_admin_dir(bare, flat)
    b = _worktree_admin_dir(bare, nested)
    assert a is not None and b is not None
    assert a != b, "git disambiguated the admin dirs; resolution must too"
    assert a.exists() and b.exists()


def test_deleting_nested_scope_leaves_flat_sibling_intact(bare_with_colliding_basenames):
    """The data-loss case: `fabfos/dev`'s basename is `dev`, and a flat `dev`
    scope owns `.bare/worktrees/dev`. Deleting one must not touch the other."""
    bare, flat, nested = bare_with_colliding_basenames
    flat_admin = _worktree_admin_dir(bare, flat)

    _cleanup_worktree(bare, nested, "fabfos/dev")

    assert not nested.exists()
    assert flat.exists()
    assert (flat / ".git").exists()
    assert flat_admin.exists(), "flat sibling's admin dir was destroyed"
    r = subprocess.run(["git", "-C", str(flat), "status"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"flat sibling broken: {r.stderr}"


def test_deleting_flat_scope_leaves_nested_sibling_intact(bare_with_colliding_basenames):
    """The same collision from the other side."""
    bare, flat, nested = bare_with_colliding_basenames
    nested_admin = _worktree_admin_dir(bare, nested)

    _cleanup_worktree(bare, flat, "feat/dev")

    assert not flat.exists()
    assert nested.exists()
    assert nested_admin.exists(), "nested sibling's admin dir was destroyed"
    r = subprocess.run(["git", "-C", str(nested), "status"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"nested sibling broken: {r.stderr}"
