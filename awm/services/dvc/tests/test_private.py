"""Private marks: a peer caller sees no worktree, and reaches no scope, a mark covers."""

from __future__ import annotations

import os
import subprocess

import pytest

from awm.config import privacy
from awm.dvc import coverage, scopedata
from awm.dvc.cache import DvcScopeError
from awm.dvc.hub_adapter import HANDLERS


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo)})


@pytest.fixture()
def marked(dvc_workspace, monkeypatch):
    root, projects = dvc_workspace["workspace"], dvc_workspace["projects_dir"]
    scopes = {}
    for project in ("proj", "avatar"):
        scope = projects / project / "dev"
        scope.mkdir(parents=True)
        git(scope, "init", "-q", "-b", "feat/dev")
        (scope / "README").write_text("x\n")
        scopes[project] = scope
    cache = root / "data" / ".dvc_cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(coverage, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(coverage, "SHARED_CACHE", cache)
    privacy.write_marks([{"project": "avatar", "scope": None, "path": str(projects / "avatar")}])
    return scopes


def _listed(as_):
    out = HANDLERS["coverage"]({"all": True}, as_)
    return [w["scope"] for w in out["worktrees"]], out


@pytest.mark.parametrize("as_", ["peer", "peer:altair"])
def test_a_peer_coverage_lists_no_private_worktree(marked, as_):
    listed, out = _listed(as_)
    assert listed == ["projects/proj/dev"]
    assert out["scopes"] == 1 and out["scopes_at_risk"] == 1


@pytest.mark.parametrize("as_", [None, "user:tony"])
def test_a_local_coverage_lists_every_worktree(marked, as_):
    assert _listed(as_)[0] == ["projects/avatar/dev", "projects/proj/dev"]


def test_a_broken_marks_file_shows_a_peer_no_worktree(marked):
    privacy.marks_path().write_text("{broken")
    assert _listed("peer")[0] == []
    assert len(_listed(None)[0]) == 2


def test_a_peer_meets_a_private_scope_path_as_no_scope(marked):
    for verb in ("status", "resolve", "pull", "push"):
        with pytest.raises(DvcScopeError, match="not a DVC scope"):
            HANDLERS[verb]({"scope": str(marked["avatar"])}, "peer")
        with pytest.raises(DvcScopeError, match="not a DVC scope"):
            HANDLERS[verb]({"scope": "projects/avatar/dev"}, "peer")


def test_a_peer_meets_a_private_scope_name_as_no_worktree(marked):
    for verb in ("scope_status", "mount"):
        with pytest.raises(FileNotFoundError, match="No worktree directory"):
            HANDLERS[verb]({"project": "avatar", "scope": "dev"}, "peer:altair")


def test_a_peer_gc_never_names_or_reaches_a_private_project(marked, monkeypatch):
    with pytest.raises(FileNotFoundError, match="no bare repo"):
        HANDLERS["gc"]({"projects": ["proj", "avatar"]}, "peer")
    with pytest.raises(FileNotFoundError, match="no bare repo"):
        HANDLERS["gc"]({"projects": ["proj"], "exclude": ["avatar"]}, "peer")
    monkeypatch.setattr(scopedata, "_dvc_wired_projects", lambda: {"proj", "avatar"})
    out = HANDLERS["gc"]({"projects": ["proj"]}, "peer")
    assert out["result"] == "refused" and "avatar" not in str(out)


def test_a_worktree_outside_the_workspace_is_listed_by_its_absolute_path(marked, tmp_path, monkeypatch):
    outside = tmp_path / "scratch" / "wt"
    outside.mkdir(parents=True)
    git(outside, "init", "-q", "-b", "feat/x")
    found = coverage.find_worktrees
    monkeypatch.setattr(coverage, "find_worktrees", lambda root: [*found(root), outside])
    assert str(outside) in _listed(None)[0]
