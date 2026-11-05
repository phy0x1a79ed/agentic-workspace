"""Real-git tests for what blocks a scope delete (scopes.md R44)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from awm.dvcdata import data_dvc
from awm.scopes import claims
from awm.scopes import scopes as mod
from awm.scopes.models import ScopeCreateRequest, ScopeUpdateRequest

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


def test_ignored_files_never_block(scope):
    (scope / ".gitignore").write_text("secrets.env\n")
    _git(scope, "add", ".gitignore")
    _git(scope, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ignore")
    _git(scope.parent / ".bare", "push", "-q", "origin", "feat/s")
    (scope / "secrets.env").write_text("x")
    assert mod.delete_scope(PROJECT, "s").status == "deleted"


def _dvc_status(monkeypatch, state: str) -> None:
    """Make the scope a DVC repo whose one pin ``dvc status`` reports in ``state``."""
    status = {"data/raw.dvc": [{"changed outs": {"data/raw": state}}]}
    monkeypatch.setattr(data_dvc, "is_dvc_repo", lambda wt: True)
    monkeypatch.setattr(data_dvc, "chunk_pins", lambda wt: ["data/raw.dvc"])
    monkeypatch.setattr(data_dvc, "dvc_available", lambda: True)
    monkeypatch.setattr(data_dvc, "_dvc", lambda wt, *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=json.dumps(status), stderr=""))


@pytest.mark.parametrize("state", ["modified", "new"])
def test_data_that_differs_from_its_pin_blocks_unless_forced(scope, monkeypatch, state):
    _dvc_status(monkeypatch, state)
    with pytest.raises(RuntimeError, match="data differs from its pin"):
        mod.delete_scope(PROJECT, "s")
    assert scope.exists()
    out = mod.delete_scope(PROJECT, "s", force=True)
    assert out.discarded["dvc_drift"] == [f"data/raw ({state})"]


@pytest.mark.parametrize("state", ["deleted", "not in cache"])
def test_pinned_data_absent_from_disk_never_blocks(scope, monkeypatch, state):
    _dvc_status(monkeypatch, state)
    assert not (scope / "data" / "raw").exists()
    out = mod.delete_scope(PROJECT, "s")
    assert out.status == "deleted"


def test_data_on_disk_that_is_in_no_cache_blocks_unless_forced(scope, monkeypatch):
    """The bytes on disk are then the machine's only copy, whether or not they match the pin."""
    _dvc_status(monkeypatch, "not in cache")
    (scope / ".gitignore").write_text("/data/raw\n")
    _git(scope, "add", ".gitignore")
    _git(scope, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ignore")
    _git(scope.parent / ".bare", "push", "-q", "origin", "feat/s")
    (scope / "data").mkdir()
    (scope / "data" / "raw").write_text("only copy")
    with pytest.raises(RuntimeError, match=r"data/raw \(not in cache\)"):
        mod.delete_scope(PROJECT, "s")
    assert scope.exists()
    out = mod.delete_scope(PROJECT, "s", force=True)
    assert out.discarded["dvc_drift"] == ["data/raw (not in cache)"]


def test_an_uncommitted_pin_blocks_as_an_untracked_file(scope):
    (scope / "data").mkdir()
    (scope / "data" / "raw.dvc").write_text("outs:\n- md5: 0\n  path: raw\n")
    with pytest.raises(RuntimeError, match="uncommitted"):
        mod.delete_scope(PROJECT, "s")
    assert (scope / "data" / "raw.dvc").exists()


def test_a_scope_without_dvc_has_no_drift(scope):
    assert mod._dvc_drift(scope) == []


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


def test_a_kept_branch_with_commits_no_other_ref_holds_blocks_the_delete(scope):
    _commit(scope, "work")
    with pytest.raises(RuntimeError, match="no other branch or remote holds"):
        mod.delete_scope(PROJECT, "s")
    out = mod.delete_scope(PROJECT, "s", force=True)
    assert out.status == "deleted" and len(out.discarded["commits"]) == 1

def _complete_state(scope: Path) -> tuple:
    """What a refused ``complete`` must leave as it was: the record, main, and origin's main."""
    bare = scope.parent / ".bare"
    return (mod.agent_id_for_scope(PROJECT, "s", active_only=True), _git(bare, "rev-parse", "main"),
            _git(bare, "ls-remote", "origin", "main"), scope.exists())


@pytest.fixture
def mergeable(scope):
    """The scope with one commit to merge, and the project's main checkout to merge it in."""
    bare = scope.parent / ".bare"
    _git(bare, "worktree", "add", "-q", str(scope.parent / "main"), "main")
    _git(bare, "config", "user.email", "t@t")
    _git(bare, "config", "user.name", "t")
    _commit(scope, "work")
    return scope


def test_a_complete_refused_by_uncommitted_files_changes_nothing(mergeable):
    (mergeable / "draft.txt").write_text("x")
    before = _complete_state(mergeable)
    with pytest.raises(RuntimeError, match="uncommitted"):
        mod.update_scope(PROJECT, "s", ScopeUpdateRequest(merge=True, cleanup=True))
    assert _complete_state(mergeable) == before and before[0] is not None


def test_a_complete_refused_by_a_live_claim_changes_nothing(mergeable):
    claims.claim(PROJECT, "s", SESSION, pid=os.getpid())
    before = _complete_state(mergeable)
    with pytest.raises(RuntimeError, match="force does not override"):
        mod.update_scope(PROJECT, "s", ScopeUpdateRequest(merge=True, cleanup=True, force=True))
    assert _complete_state(mergeable) == before


def test_a_complete_without_merge_refused_by_unique_commits_changes_nothing(mergeable):
    before = _complete_state(mergeable)
    with pytest.raises(RuntimeError, match="no other branch or remote holds"):
        mod.update_scope(PROJECT, "s", ScopeUpdateRequest(cleanup=True))
    assert _complete_state(mergeable) == before


def test_a_complete_that_merges_its_commits_cleans_up(mergeable):
    head = _git(mergeable, "rev-parse", "HEAD")
    out = mod.update_scope(PROJECT, "s", ScopeUpdateRequest(merge=True, cleanup=True))
    bare = mergeable.parent / ".bare"
    assert out.status == "completed" and not mergeable.exists()
    assert _git(bare, "merge-base", "--is-ancestor", head, "main") == ""
    assert mod.agent_id_for_scope(PROJECT, "s", active_only=True) is None


def test_a_started_sessions_claim_blocks_a_forced_delete_until_it_stops(scope):
    """The claim reflection start_other makes: the new session's id, pid and job."""
    claims.claim(PROJECT, "s", SESSION, pid=os.getpid(), job="abcd1234")
    with pytest.raises(RuntimeError, match="force does not override"):
        mod.delete_scope(PROJECT, "s", force=True)
    assert scope.exists()
    claims.release(PROJECT, "s", SESSION)
    assert mod.delete_scope(PROJECT, "s").status == "deleted"
