"""Real-git tests for heal's two mismatch lists: worktrees with no row, rows with no worktree."""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

import shutil
import subprocess
from pathlib import Path

from awm.scopes import scopes as mod
from awm.scopes.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.scopes.operations.scopes import _handle_scope_heal

PROJECT = "proj"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def proj(scopes_workspace, tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("x\n")
    _git(seed, "add", "README")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    proj = scopes_workspace["projects_dir"] / PROJECT
    proj.mkdir()
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(proj / ".bare")],
                   check=True, capture_output=True)
    return proj


def _by_hand(proj: Path, path: Path, branch: str) -> Path:
    _git(proj / ".bare", "worktree", "add", "-q", "-b", branch, str(path), "main")
    return path


def _rows() -> list:
    return sorted((s.scope, s.status) for s in mod.search_scopes(status="all").scopes)


class TestUnregisteredWorktrees:
    def test_lists_hand_made_worktrees_at_any_depth(self, proj):
        mod.create_scope(ScopeCreateRequest(project=PROJECT, scope="known"))
        _by_hand(proj, proj / "shards", "feat/shards")
        nested = _by_hand(proj, proj / "engine" / "a", "feat/engine-a")
        (nested / ".awm").mkdir()
        (nested / ".awm" / "context.md").write_text("ctx\n")

        found = mod.audit_worktrees(PROJECT)["unregistered"]

        assert sorted((u["scope"], u["branch"], u["context_md"]) for u in found) == [
            ("engine/a", "feat/engine-a", True),
            ("shards", "feat/shards", False),
        ]

    def test_skips_claude_worktrees_outside_paths_and_missing_dirs(self, proj, tmp_path):
        _by_hand(proj, proj / ".claude" / "worktrees" / "w", "wt-w")
        _by_hand(proj, tmp_path / "outside", "feat/outside")
        shutil.rmtree(_by_hand(proj, proj / "pruned", "feat/pruned"))
        assert mod.audit_worktrees(PROJECT)["unregistered"] == []

    def test_the_main_checkout_counts_as_registered(self, proj):
        _git(proj / ".bare", "worktree", "add", "-q", str(proj / "main"), "main")
        assert "main" in {s.scope for s in mod.search_scopes(project=PROJECT).scopes}
        assert mod.audit_worktrees(PROJECT)["unregistered"] == []

    def test_a_completed_row_still_registers_its_worktree(self, proj):
        mod.create_scope(ScopeCreateRequest(project=PROJECT, scope="done"))
        mod.update_scope(PROJECT, "done", ScopeUpdateRequest())
        assert (proj / "done").is_dir()
        assert mod.audit_worktrees(PROJECT)["unregistered"] == []

    def test_a_broken_project_is_reported_and_the_pass_continues(self, proj):
        (proj.parent / "broken" / ".bare").mkdir(parents=True)
        _by_hand(proj, proj / "shards", "feat/shards")

        audit = mod.audit_worktrees()

        assert [e["project"] for e in audit["errors"]] == ["broken"]
        assert [u["scope"] for u in audit["unregistered"]] == ["shards"]


class TestOrphanedRows:
    def test_lists_a_live_row_whose_worktree_is_gone(self, proj):
        mod.create_scope(ScopeCreateRequest(project=PROJECT, scope="gone"))
        mod.create_scope(ScopeCreateRequest(project=PROJECT, scope="here"))
        shutil.rmtree(proj / "gone")

        audit = mod.audit_worktrees(PROJECT)

        assert [(o["scope"], o["status"]) for o in audit["orphaned_rows"]] == [
            ("gone", "allocated")]
        assert audit["unregistered"] == []

    def test_a_retired_row_with_no_worktree_is_not_listed(self, proj):
        mod.create_scope(ScopeCreateRequest(project=PROJECT, scope="old"))
        mod.delete_scope(PROJECT, "old")
        assert mod.audit_worktrees(PROJECT)["orphaned_rows"] == []


def test_heal_reports_both_lists_and_writes_no_rows(proj):
    mod.create_scope(ScopeCreateRequest(project=PROJECT, scope="gone"))
    shutil.rmtree(proj / "gone")
    _by_hand(proj, proj / "shards", "feat/shards")
    before = _rows()

    for dry_run in (True, False):
        result = _handle_scope_heal({"project": PROJECT, "dry_run": dry_run})
        assert [u["scope"] for u in result["unregistered_worktrees"]] == ["shards"]
        assert [o["scope"] for o in result["orphaned_rows"]] == ["gone"]
        assert result["audit_errors"] == []

    assert _rows() == before
    assert not (proj / "shards" / ".awm").exists()
