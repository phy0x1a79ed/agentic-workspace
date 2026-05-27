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
    _ensure_harness_context,
    _is_workspace_shared_agents,
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
# Harness-context wiring — _ensure_harness_context / _is_workspace_shared_agents
# ---------------------------------------------------------------------------


def _seed_template(awm_workspace, monkeypatch) -> Path:
    """Drop a scope-agents template under the workspace skills dir AND
    redirect the projects module's WORKSPACE_ROOT/SKILLS_DIR bindings so
    ``_find_template`` (used by ``_ensure_harness_context``) locates it.

    The base ``awm_workspace`` fixture patches ``awm.config`` and
    ``awm.services.scopes`` bindings, but not the lazy import inside
    ``_ensure_harness_context`` that reaches into ``awm.services.projects``.
    """
    skills_dir = awm_workspace["skills_dir"]
    templates = skills_dir / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    tmpl = templates / "scope-agents.md.template"
    tmpl.write_text("# {project}\n\nbody\n@.awm/context.md\n")
    monkeypatch.setattr("awm.services.projects.WORKSPACE_ROOT",
                        awm_workspace["workspace"])
    monkeypatch.setattr("awm.services.projects.SKILLS_DIR", skills_dir)
    return tmpl


def _git_init_with_commit(worktree: Path) -> None:
    """Initialize a real git repo with one tracked AGENTS.md commit so the
    `git ls-files --error-unmatch AGENTS.md` probe succeeds."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True, env=env)
    (worktree / "AGENTS.md").write_text("# tracked\n")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=worktree, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=worktree, check=True, env=env)


class TestHarnessContext:
    def test_creates_agents_md_when_missing(self, awm_workspace, monkeypatch, tmp_path):
        _seed_template(awm_workspace, monkeypatch)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        actions = _ensure_harness_context(worktree, project_name="proj-a")

        assert (worktree / "AGENTS.md").exists()
        assert "# proj-a" in (worktree / "AGENTS.md").read_text()
        assert actions["agents_md"] == "created"

    def test_creates_claude_symlink(self, awm_workspace, monkeypatch, tmp_path):
        _seed_template(awm_workspace, monkeypatch)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        actions = _ensure_harness_context(worktree, project_name="proj-a")

        claude_md = worktree / "CLAUDE.md"
        assert claude_md.is_symlink()
        # Relative symlink so the wiring survives renames/moves.
        assert os.readlink(claude_md) == "AGENTS.md"
        assert actions["claude_symlink"] == "created"

    def test_appends_import_line_when_context_present(self, awm_workspace, monkeypatch, tmp_path):
        _seed_template(awm_workspace, monkeypatch)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "AGENTS.md").write_text("# pre-existing\n\nbody without import")
        (worktree / ".awm").mkdir()
        (worktree / ".awm" / "context.md").write_text("scope ritual\n")

        actions = _ensure_harness_context(worktree, project_name="proj-a")

        body = (worktree / "AGENTS.md").read_text()
        assert _CONTEXT_IMPORT_LINE in body
        assert body.rstrip().endswith(_CONTEXT_IMPORT_LINE)
        assert actions["import_line"] == "appended"
        # Pre-existing content untouched.
        assert "# pre-existing" in body
        assert "body without import" in body

    def test_does_not_duplicate_import_line(self, awm_workspace, monkeypatch, tmp_path):
        _seed_template(awm_workspace, monkeypatch)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        original = f"# already wired\n\nbody\n{_CONTEXT_IMPORT_LINE}\n"
        (worktree / "AGENTS.md").write_text(original)
        (worktree / ".awm").mkdir()
        (worktree / ".awm" / "context.md").write_text("x")

        actions = _ensure_harness_context(worktree, project_name="proj-a")

        assert (worktree / "AGENTS.md").read_text() == original
        assert actions["import_line"] is None

    def test_omits_import_line_when_no_context_md(self, awm_workspace, monkeypatch, tmp_path):
        _seed_template(awm_workspace, monkeypatch)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        original = "# no context yet\n"
        (worktree / "AGENTS.md").write_text(original)
        # No .awm/context.md present.

        actions = _ensure_harness_context(worktree, project_name="proj-a")

        assert (worktree / "AGENTS.md").read_text() == original
        assert actions["import_line"] is None

    def test_idempotent_second_call(self, awm_workspace, monkeypatch, tmp_path):
        _seed_template(awm_workspace, monkeypatch)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".awm").mkdir()
        (worktree / ".awm" / "context.md").write_text("x")

        first = _ensure_harness_context(worktree, project_name="proj-a")
        second = _ensure_harness_context(worktree, project_name="proj-a")

        # First call materializes the template (which already contains the
        # import line) + creates the CLAUDE.md symlink. The import-line
        # append branch is a no-op because the template ships it.
        assert first["agents_md"] == "created"
        assert first["claude_symlink"] == "created"
        assert first["import_line"] is None
        # Second call: everything already in place.
        assert second == {
            "agents_md": None, "import_line": None, "claude_symlink": None,
        }
        # Sanity: the freshly created AGENTS.md does carry the import line.
        assert _CONTEXT_IMPORT_LINE in (worktree / "AGENTS.md").read_text()

    def test_workspace_shared_skips_import_line(self, awm_workspace, monkeypatch, tmp_path):
        """projects/awm/<scope> worktrees share .bare with the workspace root,
        so a tracked AGENTS.md is the workspace orchestrator file. The import
        line must NOT be appended (it would dangle on merge to release where
        .awm/context.md doesn't exist) — but the CLAUDE.md symlink should
        still be created."""
        _seed_template(awm_workspace, monkeypatch)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _git_init_with_commit(worktree)
        original = (worktree / "AGENTS.md").read_text()
        (worktree / ".awm").mkdir()
        (worktree / ".awm" / "context.md").write_text("x")

        actions = _ensure_harness_context(worktree, project_name="awm")

        # AGENTS.md content is untouched — that's the whole point.
        assert (worktree / "AGENTS.md").read_text() == original
        assert actions["import_line"] == "skipped:workspace-shared"
        # CLAUDE.md symlink still created — Claude Code still needs it.
        assert (worktree / "CLAUDE.md").is_symlink()
        assert actions["claude_symlink"] == "created"

    def test_is_workspace_shared_false_for_non_awm_project(self, tmp_path):
        # Even when AGENTS.md is git-tracked, a non-awm project can't be
        # the workspace orchestrator (different .bare). Helper short-circuits
        # without invoking git.
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _git_init_with_commit(worktree)

        assert _is_workspace_shared_agents(worktree, "proj-a") is False

    def test_is_workspace_shared_true_when_agents_md_tracked(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _git_init_with_commit(worktree)

        assert _is_workspace_shared_agents(worktree, "awm") is True

    def test_is_workspace_shared_false_when_agents_md_untracked(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True, env=env)
        # AGENTS.md exists on disk but never added to the index.
        (worktree / "AGENTS.md").write_text("# floating\n")

        assert _is_workspace_shared_agents(worktree, "awm") is False


# ---------------------------------------------------------------------------
# heal_scopes — DB-driven back-fill
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
    def test_skips_inactive_scopes(self, awm_workspace, monkeypatch, seeded_scopes):
        # seeded_scopes has one active + two completed.
        _seed_template(awm_workspace, monkeypatch)
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

    def test_filters_by_project(self, awm_workspace, monkeypatch, seeded_scopes, db_conn,
                                 tmp_path):
        _seed_template(awm_workspace, monkeypatch)
        # Add a second active row in proj-b.
        proj_b_wt = tmp_path / "extra" / "proj-b" / "active-2"
        proj_b_wt.mkdir(parents=True)
        _insert_active_scope(db_conn, project="proj-b", scope="active-2",
                             worktree=proj_b_wt)

        report = heal_scopes(project="proj-a")
        assert {r["project"] for r in report} == {"proj-a"}

        report_all = heal_scopes()
        assert {r["project"] for r in report_all} == {"proj-a", "proj-b"}

    def test_applies_context_wiring(self, awm_workspace, monkeypatch, seeded_scopes):
        _seed_template(awm_workspace, monkeypatch)
        # seeded_scopes created the worktree dir + a placeholder AGENTS.md.
        # Add a .awm/context.md so the import line gets appended.
        from awm.services import scopes as scopes_mod
        active = scopes_mod.list_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        (wt / ".awm").mkdir(exist_ok=True)
        (wt / ".awm" / "context.md").write_text("scope ritual\n")

        report = heal_scopes()
        assert report[0]["ok"] is True
        actions = report[0]["actions"]
        # AGENTS.md already existed (seeded), so no recreate.
        assert actions["agents_md"] is None
        assert actions["claude_symlink"] == "created"
        assert actions["import_line"] == "appended"
        assert (wt / "CLAUDE.md").is_symlink()
        assert _CONTEXT_IMPORT_LINE in (wt / "AGENTS.md").read_text()

    def test_idempotent_across_runs(self, awm_workspace, monkeypatch, seeded_scopes):
        _seed_template(awm_workspace, monkeypatch)
        from awm.services import scopes as scopes_mod
        active = scopes_mod.list_scopes(status="active").scopes[0]
        wt = Path(active.worktree)
        (wt / ".awm").mkdir(exist_ok=True)
        (wt / ".awm" / "context.md").write_text("x")

        heal_scopes()
        first_agents = (wt / "AGENTS.md").read_text()

        second = heal_scopes()
        assert second[0]["actions"] == {
            "agents_md": None, "import_line": None, "claude_symlink": None,
        }
        # AGENTS.md byte-for-byte unchanged on second pass.
        assert (wt / "AGENTS.md").read_text() == first_agents
