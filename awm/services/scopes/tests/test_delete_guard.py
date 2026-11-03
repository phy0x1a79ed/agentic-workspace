"""Real-git tests for what blocks a scope delete (scopes.md R44)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awm.scopes import claims
from awm.scopes import scopes as mod
from awm.scopes.models import ScopeCreateRequest

pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

PROJECT = "proj"
SESSION = "cc" + "a" * 32


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(worktree: Path, name: str) -> str:
    (worktree / name).write_text(name)
    _git(worktree, "add", name)
    _git(worktree, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name)
    return _git(worktree, "rev-parse", "HEAD")


@pytest.fixture
def scope(scopes_workspace, tmp_path, monkeypatch):
    """A project cloned from an origin, with one scope on feat/s pushed nowhere yet."""
    monkeypatch.setattr(claims, "waiting_sessions", lambda: set())
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "README")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(origin)], check=True, capture_output=True)
    proj = scopes_workspace["projects_dir"] / PROJECT
    proj.mkdir()
    bare = proj / ".bare"
    subprocess.run(["git", "clone", "--bare", "-q", str(origin), str(bare)], check=True, capture_output=True)
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    mod.create_scope(ScopeCreateRequest(project=PROJECT, scope="s"))
    return proj / "s"


def test_a_clean_merged_scope_deletes(scope):
    assert mod.delete_scope(PROJECT, "s").status == "deleted"
    assert not scope.exists()


def test_a_live_claim_blocks_even_with_force(scope):
    claims.claim(PROJECT, "s", SESSION, pid=os.getpid())
    with pytest.raises(RuntimeError, match="force does not override"):
        mod.delete_scope(PROJECT, "s", force=True)
    assert scope.exists()


def test_ignored_files_block_unless_forced(scope):
    (scope / ".gitignore").write_text("secrets.env\n__pycache__/\n")
    _git(scope, "add", ".gitignore")
    _git(scope, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ignore")
    _git(scope.parent / ".bare", "push", "-q", "origin", "feat/s")
    (scope / "secrets.env").write_text("x")
    (scope / "__pycache__").mkdir()
    (scope / "__pycache__" / "m.pyc").write_text("x")
    with pytest.raises(RuntimeError, match="ignored file"):
        mod.delete_scope(PROJECT, "s")
    out = mod.delete_scope(PROJECT, "s", force=True)
    assert out.discarded["ignored"] == ["secrets.env"]


def test_ignored_awm_metadata_never_blocks(scope):
    assert (scope / ".awm").is_dir()
    assert mod._ignored_files(scope) == []


def test_unique_commits_block_a_branch_delete_unless_forced(scope):
    sha = _commit(scope, "work")
    with pytest.raises(RuntimeError, match="no other branch or remote holds"):
        mod.delete_scope(PROJECT, "s", delete_branch=True)
    out = mod.delete_scope(PROJECT, "s", force=True, delete_branch=True)
    assert [c.split()[0] for c in out.discarded["commits"]] == [sha[:len(out.discarded["commits"][0].split()[0])]]


def test_a_pushed_branch_is_not_unique(scope):
    _commit(scope, "work")
    _git(scope.parent / ".bare", "push", "-q", "origin", "feat/s")
    _git(scope.parent / ".bare", "fetch", "-q", "origin")
    assert mod._unique_commits(scope.parent / ".bare", "feat/s") == []
    assert mod.delete_scope(PROJECT, "s").status == "deleted"


def test_a_forced_delete_lists_every_uncommitted_path(scope):
    for i in range(8):
        (scope / f"f{i}.txt").write_text("x")
    out = mod.delete_scope(PROJECT, "s", force=True)
    assert len(out.discarded["uncommitted"]) == 8


def test_a_kept_branch_keeps_its_commits_so_they_do_not_block(scope):
    _commit(scope, "work")
    assert mod.delete_scope(PROJECT, "s").status == "deleted"
