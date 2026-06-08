"""Tests for scopes.repair_scope (inbox #232 backfill path)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def split_brain_scope(awm_workspace):
    """Build a real bare repo + worktree on disk, but DO NOT insert the DB row.

    Mirrors the gapseq/dev state from inbox #232: worktree, branch, and
    `.awm/context.md` present; `agents` table empty for the (project, scope)
    pair.
    """
    projects_dir = awm_workspace["projects_dir"]
    project = "ghostproj"
    scope = "dev"

    # Seed a one-commit bare repo.
    seed = projects_dir / project / "_seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("x")
    _git(seed, "add", "README")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    bare = projects_dir / project / ".bare"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True, capture_output=True,
    )

    worktree = projects_dir / project / scope
    _git(bare, "worktree", "add", "-b", f"feat/{scope}", str(worktree), "main")

    awm_dir = worktree / ".awm"
    awm_dir.mkdir()
    (awm_dir / "context.md").write_text(f"# {project}/{scope}\n\n(seeded by test)\n")

    return {"project": project, "scope": scope, "worktree": worktree, "bare": bare}


def test_repair_backfills_missing_db_row(awm_workspace, split_brain_scope):
    from awm.services import scopes

    # Sanity: no DB row before repair.
    pre = scopes.search_scopes(status="all", project=split_brain_scope["project"])
    assert pre.total == 0

    result = scopes.repair_scope(split_brain_scope["project"], split_brain_scope["scope"])
    assert result.status == "repaired"
    assert split_brain_scope["scope"] in result.message

    # DB row visible now.
    post = scopes.search_scopes(status="all", project=split_brain_scope["project"])
    assert post.total == 1
    row = post.scopes[0]
    assert row.scope == split_brain_scope["scope"]
    assert row.branch == f"feat/{split_brain_scope['scope']}"
    assert str(split_brain_scope["worktree"]) in row.worktree


def test_repair_is_idempotent(awm_workspace, split_brain_scope):
    from awm.services import scopes

    first = scopes.repair_scope(split_brain_scope["project"], split_brain_scope["scope"])
    assert first.status == "repaired"

    second = scopes.repair_scope(split_brain_scope["project"], split_brain_scope["scope"])
    assert second.status == "skipped"
    assert "already" in second.message.lower()


def test_repair_rejects_missing_worktree(awm_workspace):
    from awm.services import scopes

    # Make a bare so the project check passes, but no worktree dir.
    bare = awm_workspace["projects_dir"] / "p1" / ".bare"
    bare.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="Worktree not found"):
        scopes.repair_scope("p1", "nope")


def test_repair_rejects_worktree_without_context(awm_workspace, split_brain_scope):
    from awm.services import scopes

    # Delete .awm/context.md so the on-disk evidence is incomplete.
    (split_brain_scope["worktree"] / ".awm" / "context.md").unlink()

    with pytest.raises(FileNotFoundError, match="context.md"):
        scopes.repair_scope(split_brain_scope["project"], split_brain_scope["scope"])


def test_repair_rejects_missing_bare(awm_workspace):
    from awm.services import scopes

    with pytest.raises(FileNotFoundError, match="bare repo"):
        scopes.repair_scope("never-existed", "dev")
