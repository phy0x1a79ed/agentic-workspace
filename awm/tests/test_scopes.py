"""Tests for awm.services.scopes — list (DB-only) + mocked create/update/delete."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awm.db import get_connection
from awm.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.services import scopes
from awm.services.scopes import (
    _CONTEXT_IMPORT_LINE,
    _heal_worktree,
    _is_tracked,
    _strip_context_import,
    heal_scopes,
)


class TestListScopes:
    def test_list_empty(self, awm_workspace):
        result = scopes.list_scopes()
        assert result.total == 0
        assert result.scopes == []

    def test_list_all(self, awm_workspace, seeded_scopes):
        result = scopes.list_scopes()
        assert result.total == 3

    def test_list_by_status(self, awm_workspace, seeded_scopes):
        result = scopes.list_scopes(status="active")
        assert result.total == 1
        assert result.scopes[0].status == "active"

    def test_list_by_project(self, awm_workspace, seeded_scopes):
        result = scopes.list_scopes(project="proj-a")
        assert result.total == 2

    def test_list_combined_filters(self, awm_workspace, seeded_scopes):
        result = scopes.list_scopes(status="completed", project="proj-b")
        assert result.total == 1
        assert result.scopes[0].scope == "scope-3"

    def test_list_status_all(self, awm_workspace, seeded_scopes):
        result = scopes.list_scopes(status="all")
        assert result.total == 3

    def test_list_includes_repo_path(self, awm_workspace, seeded_scopes):
        result = scopes.list_scopes(status="active")
        assert result.scopes[0].repo_path is not None
        assert "projects/" in result.scopes[0].repo_path


class TestCreateScope:
    def test_create_missing_project(self, awm_workspace):
        req = ScopeCreateRequest(project="nope", scope="s1")
        with pytest.raises(FileNotFoundError, match="not found"):
            scopes.create_scope(req)


class TestUpdateScope:
    def test_complete_scope(self, awm_workspace, seeded_scopes, monkeypatch):
        monkeypatch.setattr(
            "awm.git_utils.run_git",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=1),
        )
        # Fake a bare dir so the check passes
        bare_dir = awm_workspace["projects_dir"] / "proj-a" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)

        req = ScopeUpdateRequest(action="complete")
        result = scopes.update_scope("proj-a", "scope-1", req)
        assert result.status == "completed"

        # Verify DB was updated
        db_result = scopes.list_scopes(status="completed")
        completed = [s.scope for s in db_result.scopes]
        assert "scope-1" in completed

    def test_update_missing_project(self, awm_workspace):
        req = ScopeUpdateRequest(action="complete")
        with pytest.raises(FileNotFoundError):
            scopes.update_scope("nope", "nope", req)

    def test_invalid_action_rejected(self, awm_workspace, seeded_scopes):
        with pytest.raises(Exception):
            ScopeUpdateRequest(action="pause")


class TestDeleteScope:
    def test_delete_scope(self, awm_workspace, seeded_scopes, monkeypatch):
        monkeypatch.setattr(
            "awm.git_utils.run_git",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=0),
        )
        bare_dir = awm_workspace["projects_dir"] / "proj-a" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)

        result = scopes.delete_scope("proj-a", "scope-1")
        assert result.status == "deleted"

        # Verify DB was updated
        db_result = scopes.list_scopes(status="deleted")
        deleted = [s.scope for s in db_result.scopes]
        assert "scope-1" in deleted

    def test_delete_missing_scope(self, awm_workspace):
        bare_dir = awm_workspace["projects_dir"] / "nope" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            scopes.delete_scope("nope", "nope")


# ---------------------------------------------------------------------------
# Tier-3 cleanup — _strip_context_import / _is_tracked / _heal_worktree
# ---------------------------------------------------------------------------


def _git_init_tracked_agents(worktree: Path, body: str = "# tracked\n") -> None:
    """Init a real git repo with AGENTS.md committed at HEAD."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True, env=env)
    (worktree / "AGENTS.md").write_text(body)
    subprocess.run(["git", "add", "AGENTS.md"], cwd=worktree, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=worktree, check=True, env=env)


def _git_init_no_track(worktree: Path) -> None:
    """Init a real git repo with nothing committed."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True, env=env)


class TestStripContextImport:
    def test_strips_trailing_line(self):
        text = "# title\n\nbody\n\n@.awm/context.md\n"
        new, changed = _strip_context_import(text)
        assert changed is True
        assert new == "# title\n\nbody\n"

    def test_no_op_when_absent(self):
        text = "# title\n\nbody\n"
        new, changed = _strip_context_import(text)
        assert changed is False
        assert new == text

    def test_ignores_middle_occurrence(self):
        # Only trailing @-imports are stripped — middle ones are legitimate.
        text = "# title\n@.awm/context.md\nbody\n"
        new, changed = _strip_context_import(text)
        assert changed is False
        assert new == text

    def test_no_orphan_blank_line(self):
        text = "# title\nbody\n@.awm/context.md\n"
        new, changed = _strip_context_import(text)
        assert changed is True
        # No double-blank tail left behind.
        assert new == "# title\nbody\n"


class TestIsTracked:
    def test_true_for_tracked(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        assert _is_tracked(wt, "AGENTS.md") is True

    def test_false_for_untracked(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_no_track(wt)
        (wt / "AGENTS.md").write_text("# floating\n")
        assert _is_tracked(wt, "AGENTS.md") is False

    def test_false_for_missing(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_no_track(wt)
        assert _is_tracked(wt, "AGENTS.md") is False


class TestHealWorktree:
    def test_strips_leaked_import_from_tracked_agents(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt, body="# project AGENTS\nbody\n")
        # Simulate the #216 leak: append @-import to the tracked file.
        (wt / "AGENTS.md").write_text(
            "# project AGENTS\nbody\n\n@.awm/context.md\n"
        )

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["import_line"] == "stripped"
        assert (wt / "AGENTS.md").read_text() == "# project AGENTS\nbody\n"
        assert actions["agents_md"] is None  # tracked file kept

    def test_deletes_untracked_agents_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_no_track(wt)
        (wt / "AGENTS.md").write_text("# scope-local chrome\n")

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["agents_md"] == "deleted:untracked"
        assert not (wt / "AGENTS.md").exists()

    def test_deletes_claude_symlink_to_agents(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["claude_md"] == "deleted:symlink"
        assert not (wt / "CLAUDE.md").exists()
        assert not (wt / "CLAUDE.md").is_symlink()

    def test_keeps_tracked_claude_md(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=wt, check=True, env=env)
        (wt / "CLAUDE.md").write_text("# real tracked claude\n")
        subprocess.run(["git", "add", "CLAUDE.md"], cwd=wt, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=wt, check=True, env=env)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["claude_md"] is None
        assert (wt / "CLAUDE.md").read_text() == "# real tracked claude\n"

    def test_backfills_missing_context_md(self, tmp_path, awm_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["context_md"] == "created"
        assert (wt / ".awm" / "context.md").exists()
        body = (wt / ".awm" / "context.md").read_text()
        assert "proj-a/s1" in body

    def test_refreshes_stale_opencode_config(self, tmp_path, awm_workspace):
        # Existing .awm/mcp-opencode.json with a stale `instructions` array
        # gets rewritten to the canonical shape.
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (awm_workspace["workspace"] / "WORKSPACE.md").write_text("# orient\n")
        awm = wt / ".awm"
        awm.mkdir()
        (awm / "mcp-opencode.json").write_text(
            '{"$schema":"x","mcp":{},"instructions":["legacy.md"]}\n'
        )

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["opencode_config"] == "rewritten"
        import json
        cfg = json.loads((awm / "mcp-opencode.json").read_text())
        assert cfg["instructions"] == [
            str(awm_workspace["workspace"] / "WORKSPACE.md"),
            ".awm/context.md",
        ]

    def test_creates_missing_opencode_config(self, tmp_path, awm_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (awm_workspace["workspace"] / "WORKSPACE.md").write_text("# orient\n")

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["opencode_config"] == "created"
        assert (wt / ".awm" / "mcp-opencode.json").is_file()

    def test_opencode_config_dry_run_reports_would_create(self, tmp_path, awm_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=True)

        assert actions["opencode_config"] == "would-create"
        assert not (wt / ".awm" / "mcp-opencode.json").exists()

    def test_leaves_existing_context_md_alone(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (wt / ".awm").mkdir()
        original = "# my custom context\n"
        (wt / ".awm" / "context.md").write_text(original)

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert actions["context_md"] is None
        assert (wt / ".awm" / "context.md").read_text() == original

    def test_dry_run_does_not_mutate(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt, body="# project\n")
        (wt / "AGENTS.md").write_text("# project\n\n@.awm/context.md\n")
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        before_agents = (wt / "AGENTS.md").read_text()

        actions = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=True)

        assert actions["import_line"] == "stripped"
        assert actions["claude_md"] == "deleted:symlink"
        assert actions["context_md"] == "created"
        # No mutation actually applied.
        assert (wt / "AGENTS.md").read_text() == before_agents
        assert (wt / "CLAUDE.md").is_symlink()
        assert not (wt / ".awm" / "context.md").exists() if (wt / ".awm").exists() else True

    def test_idempotent(self, tmp_path, awm_workspace):
        wt = tmp_path / "wt"
        wt.mkdir()
        _git_init_tracked_agents(wt)
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)
        # Snapshot after first heal.
        agents_after = (wt / "AGENTS.md").read_text()
        ctx_after = (wt / ".awm" / "context.md").read_text()

        second = _heal_worktree(wt, project="proj-a", scope="s1", dry_run=False)

        assert second == {
            "import_line": None, "agents_md": None,
            "claude_md": None, "context_md": None,
            "opencode_config": None,
        }
        assert (wt / "AGENTS.md").read_text() == agents_after
        assert (wt / ".awm" / "context.md").read_text() == ctx_after


# ---------------------------------------------------------------------------
# heal_scopes — DB-driven cleanup pass
# ---------------------------------------------------------------------------


def _insert_active_scope(db_conn, *, project: str, scope: str,
                        worktree: Path) -> None:
    """Append an active scope row mirroring the seeded_scopes shape."""
    from awm.services.replication.schema import new_uuid
    now = datetime.now(timezone.utc).isoformat()
    db_conn.execute(
        "INSERT INTO scopes "
        "(uuid, legacy_id, origin_peer, project, scope, status, branch, "
        " worktree, repo_path, session, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_uuid(), 99, "test-host", project, scope, "active",
         f"feat/{scope}", str(worktree), str(worktree.parent), 1, now, now),
    )
    db_conn.commit()


class TestHealScopes:
    def test_skips_inactive_scopes(self, awm_workspace, seeded_scopes):
        # seeded_scopes has one active + two completed.
        report = heal_scopes()
        assert len(report) == 1
        assert report[0]["scope"] == "scope-1"
        assert report[0]["ok"] is True

    def test_reports_missing_worktree(self, awm_workspace, seeded_scopes,
                                       db_conn):
        # Point the active scope at a nonexistent path.
        bogus = "/tmp/awm-test-does-not-exist-xyz"
        db_conn.execute(
            "UPDATE scopes SET worktree=? WHERE scope='scope-1'", (bogus,),
        )
        db_conn.commit()
        report = heal_scopes()
        assert len(report) == 1
        assert report[0]["ok"] is False
        assert report[0]["error"] == "worktree missing"

    def test_filters_by_project(self, awm_workspace, seeded_scopes, db_conn,
                                 tmp_path):
        # Add a second active row in proj-b.
        proj_b_wt = tmp_path / "extra" / "proj-b" / "active-2"
        proj_b_wt.mkdir(parents=True)
        _insert_active_scope(db_conn, project="proj-b", scope="active-2",
                             worktree=proj_b_wt)

        report = heal_scopes(project="proj-a")
        assert {r["project"] for r in report} == {"proj-a"}

        report_all = heal_scopes()
        assert {r["project"] for r in report_all} == {"proj-a", "proj-b"}

    def test_cleans_up_active_worktree(self, awm_workspace, seeded_scopes):
        # seeded_scopes wrote an untracked AGENTS.md at the worktree (no git
        # init). The cleanup pass should delete it and backfill .awm/context.md.
        from awm.services import scopes as scopes_mod
        active = scopes_mod.list_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        # Need a git repo for _is_tracked to work; init an empty one — AGENTS.md
        # remains untracked.
        _git_init_no_track(wt)
        # Add a CLAUDE.md symlink to mirror real worktree state.
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        report = heal_scopes()

        assert report[0]["ok"] is True
        actions = report[0]["actions"]
        assert actions["agents_md"] == "deleted:untracked"
        assert actions["claude_md"] == "deleted:symlink"
        assert actions["context_md"] == "created"
        assert not (wt / "AGENTS.md").exists()
        assert not (wt / "CLAUDE.md").exists()
        assert (wt / ".awm" / "context.md").exists()

    def test_dry_run_reports_without_mutating(self, awm_workspace, seeded_scopes):
        from awm.services import scopes as scopes_mod
        active = scopes_mod.list_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        _git_init_no_track(wt)
        (wt / "CLAUDE.md").symlink_to("AGENTS.md")

        report = heal_scopes(dry_run=True)

        assert report[0]["dry_run"] is True
        actions = report[0]["actions"]
        assert actions["agents_md"] == "deleted:untracked"
        # Nothing actually deleted.
        assert (wt / "AGENTS.md").exists()
        assert (wt / "CLAUDE.md").is_symlink()

    def test_idempotent_across_runs(self, awm_workspace, seeded_scopes):
        from awm.services import scopes as scopes_mod
        active = scopes_mod.list_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        _git_init_no_track(wt)

        heal_scopes()
        second = heal_scopes()
        assert second[0]["actions"] == {
            "import_line": None, "agents_md": None,
            "claude_md": None, "context_md": None,
            "opencode_config": None,
        }
