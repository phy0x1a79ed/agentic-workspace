"""Tests for scopes.gather_scope / scatter_scope (fan-in / fan-out merges).

Builds a real on-disk bare repo with a hub worktree (legacy flat branch
``dev``) and two peripheral ``feat/*`` worktrees, plus matching DB rows, then
drives the batch merges and asserts the per-peripheral report + the actual
git state (including a conflict that is aborted and leaves a clean worktree).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True, text=True, check=True,
    )


def _commit(worktree: Path, name: str, content: str, msg: str) -> None:
    (worktree / name).write_text(content)
    _git(worktree, "add", name)
    _git(worktree, "commit", "-q", "-m", msg)


@pytest.fixture
def hub_repo(scopes_workspace):
    """A bare repo with a hub scope (flat branch ``dev``) and two peripheral
    ``feat/*`` worktrees, all backed by DB rows."""
    from awm.scopes.identity import ensure_project, ensure_agent

    projects_dir = scopes_workspace["projects_dir"]
    project = "fanproj"

    # Seed a one-commit bare repo on `main`.
    seed = projects_dir / project / "_seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "base.txt")
    _git(seed, "commit", "-q", "-m", "init")

    bare = projects_dir / project / ".bare"
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(bare)],
                   check=True, capture_output=True)

    # Hub: legacy flat branch `dev` (NOT feat/dev) — exercises DB-row resolution.
    hub_wt = projects_dir / project / "dev"
    _git(bare, "worktree", "add", "-b", "dev", str(hub_wt), "main")

    # Peripherals on feat/* branches.
    p_wts = {}
    for p in ("p1", "p2"):
        wt = projects_dir / project / p
        _git(bare, "worktree", "add", "-b", f"feat/{p}", str(wt), "main")
        p_wts[p] = wt

    # DB rows so identity resolution finds the right branch/worktree.
    from awm.persistence.databases import get_connection
    conn = get_connection("scopes")
    try:
        ensure_project(project, repo_path=str(bare), conn=conn)
        ensure_agent(project, "dev", branch="dev", worktree=str(hub_wt),
                     status="active", conn=conn)
        for p, wt in p_wts.items():
            ensure_agent(project, p, branch=f"feat/{p}", worktree=str(wt),
                         status="active", conn=conn)
        conn.commit()
    finally:
        conn.close()

    return {"project": project, "bare": bare, "hub_wt": hub_wt, "p_wts": p_wts}


def _results_by_scope(resp) -> dict:
    return {r["scope"]: r for r in resp.results}


def test_gather_clean_fan_in(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    _commit(hub_repo["p_wts"]["p1"], "p1.txt", "from p1\n", "p1 work")
    _commit(hub_repo["p_wts"]["p2"], "p2.txt", "from p2\n", "p2 work")

    resp = scopes.gather_scope(hub_repo["project"], "dev", ["p1", "p2"])
    assert resp.direction == "gather"
    assert resp.hub_branch == "dev"
    by = _results_by_scope(resp)
    assert by["p1"]["result"] == "merged"
    assert by["p2"]["result"] == "merged"
    # Both peripherals' files are now on the hub worktree.
    assert (hub_repo["hub_wt"] / "p1.txt").exists()
    assert (hub_repo["hub_wt"] / "p2.txt").exists()


def test_scatter_clean_fan_out(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    _commit(hub_repo["hub_wt"], "hub.txt", "from hub\n", "hub work")

    resp = scopes.scatter_scope(hub_repo["project"], "dev", ["p1", "p2"])
    assert resp.direction == "scatter"
    by = _results_by_scope(resp)
    assert by["p1"]["result"] == "merged"
    assert by["p2"]["result"] == "merged"
    assert (hub_repo["p_wts"]["p1"] / "hub.txt").exists()
    assert (hub_repo["p_wts"]["p2"] / "hub.txt").exists()


def test_gather_up_to_date_noop(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    _commit(hub_repo["p_wts"]["p1"], "p1.txt", "from p1\n", "p1 work")
    first = scopes.gather_scope(hub_repo["project"], "dev", ["p1"])
    assert _results_by_scope(first)["p1"]["result"] == "merged"

    second = scopes.gather_scope(hub_repo["project"], "dev", ["p1"])
    assert _results_by_scope(second)["p1"]["result"] == "up_to_date"


def test_gather_conflict_aborted_batch_continues(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    # p1 and the hub edit the SAME line of base.txt → conflict on gather.
    _commit(hub_repo["hub_wt"], "base.txt", "hub edit\n", "hub edits base")
    _commit(hub_repo["p_wts"]["p1"], "base.txt", "p1 edit\n", "p1 edits base")
    # p2 touches a different file → clean merge.
    _commit(hub_repo["p_wts"]["p2"], "p2.txt", "from p2\n", "p2 work")

    resp = scopes.gather_scope(hub_repo["project"], "dev", ["p1", "p2"])
    by = _results_by_scope(resp)
    assert by["p1"]["result"] == "conflict"
    assert by["p2"]["result"] == "merged"

    # Hub worktree left clean — no lingering merge state.
    status = _git(hub_repo["hub_wt"], "status", "--porcelain").stdout
    assert status.strip() == ""
    merge_head = subprocess.run(
        ["git", "-C", str(hub_repo["hub_wt"]), "rev-parse", "--verify", "-q", "MERGE_HEAD"],
        capture_output=True, text=True,
    )
    assert merge_head.returncode != 0  # no MERGE_HEAD


def test_gather_skips_unknown_peripheral(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    resp = scopes.gather_scope(hub_repo["project"], "dev", ["nope"])
    by = _results_by_scope(resp)
    assert by["nope"]["result"] == "skipped"


def test_gather_refuses_dirty_hub(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    (hub_repo["hub_wt"] / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        scopes.gather_scope(hub_repo["project"], "dev", ["p1"])


def test_scatter_skips_dirty_peripheral(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    _commit(hub_repo["hub_wt"], "hub.txt", "from hub\n", "hub work")
    (hub_repo["p_wts"]["p1"] / "dirty.txt").write_text("uncommitted\n")

    resp = scopes.scatter_scope(hub_repo["project"], "dev", ["p1", "p2"])
    by = _results_by_scope(resp)
    assert by["p1"]["result"] == "skipped"
    assert by["p2"]["result"] == "merged"


def test_rebase_strategy_rejected(scopes_workspace, hub_repo):
    from awm.scopes import scopes

    with pytest.raises(ValueError, match="merge"):
        scopes.gather_scope(hub_repo["project"], "dev", ["p1"], strategy="rebase")
