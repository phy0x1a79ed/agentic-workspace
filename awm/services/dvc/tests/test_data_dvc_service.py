"""The dvc service's verbs over the data layer in :mod:`awm.dvcdata.data_dvc`.

The layer's own properties are tested with it, in the dvcdata library. These
are the ones that need this service: a project that keeps no remote is refused
by push and pull and left out of the shared-cache gc, and the gc verb finds a
wired project.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awm.dvcdata import data_dvc as dd

from awm.dvc import scopedata

needs_dvc = pytest.mark.skipif(not dd.dvc_available(), reason="dvc not installed")

TAG = "Signature: 8a477f597d28d172789f06886806bc55\n# no remote\n"


def _sh(*args, cwd=None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return (r.stdout or "") + (r.stderr or "")


def _git(repo: Path, *args: str) -> str:
    return _sh("git", "-C", str(repo), *args)


@pytest.fixture()
def dvc_project(dvc_workspace, tmp_path):
    """A bare repo + one secondary worktree, DVC-initialised against a shared cache."""
    projects = dvc_workspace["workspace"] / "projects"
    bare = projects / "p" / ".bare"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _sh("git", "init", "-q", "--bare", "-b", "main", str(bare))

    seed = tmp_path / "seed"
    _sh("git", "clone", "-q", str(bare), str(seed))
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "main")

    wt = projects / "p" / "main"
    _sh("git", "-C", str(bare), "worktree", "add", "-q", str(wt), "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    _sh(dd.dvc_bin(), "init", "-q", cwd=str(wt))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "dvc init")
    return {"bare": bare, "worktree": wt, "workspace": dvc_workspace["workspace"]}


@needs_dvc
@pytest.mark.parametrize("verb", ["push", "pull"])
def test_push_and_pull_refuse_a_tagged_project(dvc_project, monkeypatch, verb):
    from awm.dvc import cache as cachemod
    from awm.dvc import config as dvc_config
    from awm.dvc import transfer

    wt = dvc_project["worktree"]
    (dvc_project["bare"].parent / "CACHEDIR.TAG").write_text(TAG)
    dd.ensure_cache_config(wt)
    monkeypatch.setattr(dvc_config, "WORKSPACE_ROOT", dvc_project["workspace"])
    monkeypatch.setattr(dvc_config, "load", lambda: None)
    with pytest.raises(cachemod.DvcScopeError, match="keeps no remote"):
        getattr(transfer, verb)(str(wt), dry_run=True)


@needs_dvc
def test_a_tagged_project_is_not_wired_to_the_shared_cache(dvc_project):
    wt = dvc_project["worktree"]
    dd.ensure_repo_wiring(wt)
    assert "p" in scopedata._dvc_wired_projects()
    (dvc_project["bare"].parent / "CACHEDIR.TAG").write_text(TAG)
    assert "p" not in scopedata._dvc_wired_projects()


@needs_dvc
def test_data_gc_finds_the_wired_project(dvc_project):
    wt, bare = dvc_project["worktree"], dvc_project["bare"]
    dd.provision_scope_data("p", "main", wt)
    wt2 = bare.parent / "other"
    _git(bare, "worktree", "add", "-q", "-b", "other", str(wt2), "main")
    dd.ensure_cache_config(wt2)

    assert scopedata.gc([])["result"] == "refused"
    rep = scopedata.gc(["p"])
    assert rep["result"] == "ok", rep
    assert rep["projects"]["p"]["worktrees"] == 2
