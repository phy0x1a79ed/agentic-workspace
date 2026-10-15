"""The scope-data verbs resolve a scope from the workspace layout and git alone."""

from __future__ import annotations

import subprocess

import pytest

from awm.dvc import hub_adapter as ha
from awm.dvc import scopedata

FUNCS = {f["name"]: f for f in ha.API_MANIFEST["functions"]}


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def project(dvc_workspace):
    """A bare repo with a flat worktree and a nested one."""
    projects = dvc_workspace["projects_dir"]
    bare = projects / "p" / ".bare"
    bare.parent.mkdir()
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    seed = projects.parent / "seed"
    _git("clone", "-q", str(bare), str(seed))
    _git("-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "seed")
    _git("-C", str(seed), "push", "-q", "origin", "main")
    _git("-C", str(bare), "worktree", "add", "-q", "-b", "a", str(projects / "p" / "a"), "main")
    _git("-C", str(bare), "worktree", "add", "-q", "-b", "n", str(projects / "p" / "fab" / "dev"), "main")
    return projects / "p"


def test_the_three_verbs_are_declared_and_handled():
    for verb in ("scope_status", "mount", "gc"):
        assert verb in FUNCS and verb in ha.HANDLERS
        assert FUNCS[verb]["tool"] == f"dvc_{verb}"
        assert "surfaces" not in FUNCS[verb]


def test_scope_status_reads_and_the_others_write():
    assert FUNCS["scope_status"]["effect"] == "read"
    assert FUNCS["mount"]["effect"] == "write"
    assert FUNCS["gc"]["effect"] == "write"


def test_gc_still_requires_the_caller_to_name_its_projects():
    gc = FUNCS["gc"]
    assert [p["required"] for p in gc["params"] if p["name"] == "projects"] == [True]
    assert "DRY RUN BY DEFAULT" in gc["description"]


def test_service_toml_names_the_tool_and_turns_it_on():
    import tomllib
    from pathlib import Path

    toml = tomllib.loads(
        (Path(ha.__file__).parents[2] / "service.toml").read_text())
    assert toml == {"tool": "dvc", "tier": "on"}


@pytest.mark.parametrize("bad", ["", "../x", ".hidden", "a\\b", "a/b", "a\x00"])
def test_a_project_name_that_could_escape_is_refused(bad):
    with pytest.raises(ValueError):
        scopedata.scope_status(bad, "s")


@pytest.mark.parametrize("bad", ["", "../x", "a/../b", "a//b", "/abs"])
def test_a_scope_name_that_could_escape_is_refused(bad):
    with pytest.raises(ValueError):
        scopedata.scope_status("p", bad)


def test_worktrees_are_enumerated_at_any_depth(project):
    found = {p.resolve() for p in scopedata.project_worktrees("p")}
    assert {(project / "a").resolve(), (project / "fab" / "dev").resolve()} <= found
    assert (project / ".bare").resolve() not in found


def test_an_unknown_project_raises_rather_than_returning_nothing(dvc_workspace):
    with pytest.raises(FileNotFoundError):
        scopedata.project_worktrees("nope")


def test_scope_status_of_a_plain_nested_scope_reports_missing(project):
    rep = scopedata.scope_status("p", "fab/dev")
    assert rep["mode"] == "missing"


def test_gc_with_nothing_listed_is_refused_not_run(project):
    assert scopedata.gc([])["result"] == "refused"


def test_a_scope_with_no_worktree_directory_raises_naming_the_path(project):
    with pytest.raises(FileNotFoundError, match="projects/p/ghost"):
        scopedata.scope_status("p", "ghost")
    with pytest.raises(FileNotFoundError, match="projects/p/ghost"):
        scopedata.mount("p", "ghost", ["data/x"])


def test_a_directory_that_is_not_a_registered_worktree_raises(project):
    (project / "stray").mkdir()
    with pytest.raises(ValueError, match="stray"):
        scopedata.scope_status("p", "stray")
    assert not (project / "stray" / ".awm").exists()


def test_mount_does_not_write_into_an_unregistered_directory(project):
    (project / "stray").mkdir()
    with pytest.raises(ValueError, match="stray"):
        scopedata.mount("p", "stray", ["data/x"])
    assert not (project / "stray" / ".awm").exists()


def test_a_detached_worktree_is_not_taken_for_a_scope(project):
    _git("-C", str(project / "a"), "checkout", "-q", "--detach")
    with pytest.raises(ValueError, match="detached"):
        scopedata.scope_status("p", "a")


def test_gc_refuses_the_whole_collection_when_a_worktree_is_gone(project):
    import shutil

    shutil.rmtree(project / "fab" / "dev")
    rep = scopedata.gc(["p"])
    assert rep["result"] == "refused"
    assert str(project / "fab" / "dev") in rep["detail"]
    assert "deleted_anything" not in rep
