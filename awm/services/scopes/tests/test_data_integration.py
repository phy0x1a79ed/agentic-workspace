"""End-to-end: the data layer as reached through the scopes service.

test_data_annex.py exercises the module. This file exercises the *wiring* —
that ``scope_create`` produces a clone, that ``gather``/``scatter`` with
``data=True`` reconcile it, and that teardown refuses to eat the only copy of
something. Real bare repo, real worktrees, real git-annex.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awm.scopes import data_annex as da

pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess,
              pytest.mark.skipif(not da.annex_available(),
                                 reason="git-annex not installed")]

PROJECT = "dproj"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def project_repo(scopes_workspace):
    """A code bare repo plus a converted (annex) data directory."""
    from awm.scopes.identity import ensure_project
    from awm.persistence.databases import get_connection

    projects_dir = scopes_workspace["projects_dir"]
    seed = projects_dir / PROJECT / "_seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "base.txt")
    _git(seed, "commit", "-q", "-m", "init")

    bare = projects_dir / PROJECT / ".bare"
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(bare)],
                   check=True, capture_output=True)

    data = scopes_workspace["data_dir"] / PROJECT
    (data / "raw").mkdir(parents=True)
    (data / "raw" / "bulk.bin").write_bytes(os.urandom(300_000))
    assert da.init_project_data(PROJECT)["result"] == "converted"

    conn = get_connection("scopes")
    try:
        ensure_project(PROJECT, repo_path=str(bare), conn=conn)
        conn.commit()
    finally:
        conn.close()
    return {"bare": bare, "data": data, "projects_dir": projects_dir}


def _create(scope: str):
    from awm.scopes import scopes
    from awm.scopes.models import ScopeCreateRequest
    return scopes.create_scope(ScopeCreateRequest(project=PROJECT, scope=scope))


def test_scope_create_produces_a_clone_not_a_symlink(scopes_workspace, project_repo):
    from awm.scopes import scopes
    _create("s1")
    wt = project_repo["projects_dir"] / PROJECT / "s1"

    assert not (wt / ".awm" / "data").is_symlink()
    st = scopes.data_status(PROJECT, "s1")
    assert st["mode"] == "annex"
    assert st["branch"] == "scope/s1"
    # The data the project already had is readable from the scope.
    assert (wt / ".awm" / "data" / "raw" / "bulk.bin").exists()


def test_clone_stays_invisible_to_the_code_repo(scopes_workspace, project_repo):
    """The whole reason for putting it at the gitignored .awm path."""
    _create("s1")
    wt = project_repo["projects_dir"] / PROJECT / "s1"
    (wt / ".awm" / "data" / "out.bin").write_bytes(os.urandom(200_000))

    status = _git(wt, "status", "--porcelain").stdout
    assert status.strip() == "", f"code worktree went dirty: {status}"
    assert not (wt / ".gitmodules").exists()


def test_snapshot_and_promote_through_the_service(scopes_workspace, project_repo):
    from awm.scopes import scopes
    _create("s1")
    wt = project_repo["projects_dir"] / PROJECT / "s1"
    (wt / ".awm" / "data" / "out.bin").write_bytes(os.urandom(200_000))

    snap = scopes.data_snapshot(PROJECT, "s1")
    assert snap["result"] == "committed"
    prom = scopes.data_promote(PROJECT, "s1")
    assert prom["result"] == "promoted", prom
    # Canonical working tree moved, so absolute paths into data/ stay valid.
    assert (project_repo["data"] / "out.bin").exists()


def test_gather_with_data_fans_in_both_legs(scopes_workspace, project_repo):
    from awm.scopes import scopes
    _create("hub")
    _create("p1")
    hub_wt = project_repo["projects_dir"] / PROJECT / "hub"
    p1_wt = project_repo["projects_dir"] / PROJECT / "p1"

    # Code change on the peripheral…
    (p1_wt / "p1.txt").write_text("from p1\n")
    _git(p1_wt, "add", "p1.txt")
    _git(p1_wt, "commit", "-q", "-m", "p1 work")
    # …and a data change alongside it.
    (p1_wt / ".awm" / "data" / "p1-out.bin").write_bytes(os.urandom(200_000))
    scopes.data_snapshot(PROJECT, "p1")

    resp = scopes.gather_scope(PROJECT, "hub", ["p1"], data=True)

    assert {r["scope"]: r["result"] for r in resp.results} == {"p1": "merged"}
    assert resp.data_results is not None
    assert [r["result"] for r in resp.data_results] == ["merged"]
    assert (hub_wt / "p1.txt").exists()
    assert (hub_wt / ".awm" / "data" / "p1-out.bin").exists()


def test_scatter_with_data_fans_out(scopes_workspace, project_repo):
    from awm.scopes import scopes
    _create("hub")
    _create("p1")
    hub_wt = project_repo["projects_dir"] / PROJECT / "hub"
    p1_wt = project_repo["projects_dir"] / PROJECT / "p1"

    (hub_wt / ".awm" / "data" / "hub-out.bin").write_bytes(os.urandom(200_000))
    scopes.data_snapshot(PROJECT, "hub")

    resp = scopes.scatter_scope(PROJECT, "hub", ["p1"], data=True)

    assert [r["result"] for r in resp.data_results] == ["merged"]
    assert (p1_wt / ".awm" / "data" / "hub-out.bin").exists()


def test_delete_refuses_to_destroy_unpublished_content(scopes_workspace, project_repo):
    from awm.scopes import scopes
    _create("s1")
    wt = project_repo["projects_dir"] / PROJECT / "s1"
    (wt / ".awm" / "data" / "only-here.bin").write_bytes(os.urandom(200_000))
    scopes.data_snapshot(PROJECT, "s1")
    # Sever the route to the canonical repo so publishing cannot succeed.
    _git(wt / ".awm" / "data", "remote", "set-url", "origin",
         str(scopes_workspace["workspace"] / "gone"))

    with pytest.raises(RuntimeError, match="Refusing to remove"):
        scopes.delete_scope(PROJECT, "s1")
    assert wt.exists(), "worktree was removed despite the refusal"

    result = scopes.delete_scope(PROJECT, "s1", force=True)
    assert result.status == "deleted"
    assert not wt.exists()


def test_delete_succeeds_once_content_is_published(scopes_workspace, project_repo):
    from awm.scopes import scopes
    _create("s1")
    wt = project_repo["projects_dir"] / PROJECT / "s1"
    (wt / ".awm" / "data" / "published.bin").write_bytes(os.urandom(200_000))
    scopes.data_snapshot(PROJECT, "s1")

    assert scopes.delete_scope(PROJECT, "s1").status == "deleted"
    assert not wt.exists(), "read-only annex objects blocked the delete"
