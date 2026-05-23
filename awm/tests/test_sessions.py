"""Tests for awm.services.sessions — log, list, get."""

from __future__ import annotations

import subprocess

import pytest

from awm.models import SessionLogCreateRequest
from awm.services import sessions


class TestFormatEntry:
    def test_minimal_entry(self):
        req = SessionLogCreateRequest(project="p", scope="t", summary="Did things")
        text = sessions._format_entry(req, "2024-06-15T12:00:00+00:00")
        assert "**Date:** 2024-06-15" in text
        assert "**Scope:** p/t" in text
        assert "Did things" in text

    def test_full_entry(self):
        req = SessionLogCreateRequest(
            project="p", scope="t", summary="Summary",
            decisions=["Used pandas"], issues=["Slow query"],
            next_steps=["Optimize"], agent_id="agent-x",
        )
        text = sessions._format_entry(req, "2024-06-15T12:00:00+00:00")
        assert "## Decisions Made" in text
        assert "- Used pandas" in text
        assert "## Gotchas / Issues" in text
        assert "- Slow query" in text
        assert "## Next Steps" in text
        assert "- [ ] Optimize" in text
        assert "**Agent:** agent-x" in text

    def test_entry_ends_with_separator(self):
        req = SessionLogCreateRequest(project="p", scope="t", summary="s")
        text = sessions._format_entry(req, "2024-01-01T00:00:00+00:00")
        assert text.strip().endswith("---")

    def test_entry_with_title(self):
        req = SessionLogCreateRequest(
            project="p", scope="t", summary="Did things",
            title="Fix parser bug",
        )
        text = sessions._format_entry(req, "2024-06-15T12:00:00+00:00")
        assert text.startswith("# Fix parser bug")
        assert "Did things" in text

    def test_entry_without_title_omits_heading(self):
        req = SessionLogCreateRequest(project="p", scope="t", summary="Did things")
        text = sessions._format_entry(req, "2024-06-15T12:00:00+00:00")
        assert not text.lstrip().startswith("#")

    def test_entry_with_skill_path(self):
        req = SessionLogCreateRequest(
            project="p", scope="t", summary="s", skill_path="sops/debrief.md",
        )
        text = sessions._format_entry(req, "2024-01-01T00:00:00+00:00")
        assert "**Skill:** sops/debrief.md" in text

    def test_entry_without_skill_path_omits_line(self):
        req = SessionLogCreateRequest(project="p", scope="t", summary="s")
        text = sessions._format_entry(req, "2024-01-01T00:00:00+00:00")
        assert "**Skill:**" not in text

    def test_entry_with_outcome(self):
        req = SessionLogCreateRequest(
            project="p", scope="t", summary="s", outcome="success",
        )
        text = sessions._format_entry(req, "2024-01-01T00:00:00+00:00")
        assert "**Outcome:** success" in text

    def test_entry_with_deviations_and_suggestions(self):
        req = SessionLogCreateRequest(
            project="p", scope="t", summary="s",
            deviations="Skipped step 2", suggestions="Automate step 2",
        )
        text = sessions._format_entry(req, "2024-01-01T00:00:00+00:00")
        assert "## Deviations" in text
        assert "Skipped step 2" in text
        assert "## Suggestions" in text
        assert "Automate step 2" in text


class TestLogSession:
    def test_log_creates_db_entry(self, awm_workspace):
        """log_session creates a DB row with content, no file I/O."""
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Test session log",
        )
        entry = sessions.log_session(req)
        assert entry.project == "proj-a"
        assert entry.scope == "scope-1"
        assert entry.git_commit is None

        # Verify content in DB
        detail = sessions.get_session(entry.id)
        assert "Test session log" in detail.content

    def test_log_with_metadata(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s",
            decisions=["d1"], issues=["i1"],
        )
        entry = sessions.log_session(req)
        assert entry.id is not None

        detail = sessions.get_session(entry.id)
        assert "d1" in detail.content

    def test_log_with_skill_path(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s",
            skill_path="sops/debrief.md",
        )
        entry = sessions.log_session(req)
        assert entry.skill_path == "sops/debrief.md"

        detail = sessions.get_session(entry.id)
        assert detail.entry.skill_path == "sops/debrief.md"
        assert "**Skill:** sops/debrief.md" in detail.content

    def test_log_with_outcome_and_deviations(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Ran the pipeline",
            outcome="partial_success", deviations="Skipped step 3",
            suggestions="Add retry logic",
        )
        entry = sessions.log_session(req)
        assert entry.outcome == "partial_success"
        assert entry.deviations == "Skipped step 3"
        assert entry.suggestions == "Add retry logic"

        detail = sessions.get_session(entry.id)
        assert "**Outcome:** partial_success" in detail.content
        assert "## Deviations" in detail.content
        assert "Skipped step 3" in detail.content
        assert "## Suggestions" in detail.content
        assert "Add retry logic" in detail.content

    def test_log_with_title(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Narrative summary",
            title="Fix parser bug",
        )
        entry = sessions.log_session(req)
        assert entry.title == "Fix parser bug"

        detail = sessions.get_session(entry.id)
        assert "# Fix parser bug" in detail.content

    def test_log_with_skill_captures_version(self, awm_workspace, monkeypatch):
        monkeypatch.setattr(
            "awm.services.sessions._get_skills_git_hash",
            lambda: "abc123def456",
        )
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s",
            skill_path="awm/debrief.md",
        )
        entry = sessions.log_session(req)
        assert entry.skill_version == "abc123def456"


class TestListSessions:
    def test_list_all(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions()
        assert result.total == 3

    def test_list_by_project(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions(project="proj-a")
        assert result.total == 3

    def test_list_by_scope(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions(project="proj-a", scope="scope-1")
        assert result.total == 2

    def test_list_with_limit(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions(limit=1)
        assert result.total == 1

    def test_list_empty(self, awm_workspace):
        result = sessions.list_sessions()
        assert result.total == 0

    def test_list_by_skill_path(self, awm_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s1",
            skill_path="awm/debrief.md",
        ))
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s2",
            skill_path="awm/create-scope.md",
        ))
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s3",
        ))
        result = sessions.list_sessions(skill_path="awm/debrief.md")
        assert result.total == 1
        assert result.entries[0].skill_path == "awm/debrief.md"


class TestGetSession:
    def test_get_existing(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions()
        entry_id = result.entries[0].id
        detail = sessions.get_session(entry_id)
        assert detail.entry.id == entry_id
        assert detail.content  # content comes from DB

    def test_get_nonexistent(self, awm_workspace):
        with pytest.raises(FileNotFoundError):
            sessions.get_session(99999)

    def test_get_resolved_includes_resolution(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Bug found",
        )
        entry = sessions.log_session(req)
        sessions.resolve_session(entry.id, "Added input validation")
        detail = sessions.get_session(entry.id)
        assert "## Resolution" in detail.content
        assert "Added input validation" in detail.content


# ---------------------------------------------------------------------------
# Scope session lifecycle
# ---------------------------------------------------------------------------

class TestScopeSessions:
    """Test session numbering when re-creating completed scopes."""

    def test_create_on_completed_scope_creates_session_2(self, awm_workspace, seeded_scopes):
        """Re-creating a completed scope should increment the session number."""
        from awm.services import scopes as scope_svc
        from awm.models import ScopeCreateRequest

        projects_dir = awm_workspace["projects_dir"]

        # scope-2 is completed in seeded_scopes — set up bare repo for it
        bare_dir = projects_dir / "proj-a" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)
        # Init a real bare repo so worktree creation works
        subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True)
        # Create an initial commit so branches work
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "clone", str(bare_dir), tmp], check=True, capture_output=True)
            dummy = os.path.join(tmp, "README.md")
            with open(dummy, "w") as f:
                f.write("init")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmp, "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmp, "push"], check=True, capture_output=True)

        req = ScopeCreateRequest(project="proj-a", scope="scope-2")
        resp = scope_svc.create_scope(req)
        assert resp.session == 2
        assert resp.status == "active"

    def test_create_on_active_scope_raises(self, awm_workspace, seeded_scopes):
        """Creating a scope that already has an active session should fail."""
        from awm.services import scopes as scope_svc
        from awm.models import ScopeCreateRequest

        req = ScopeCreateRequest(project="proj-a", scope="scope-1")
        with pytest.raises(FileExistsError, match="active session"):
            scope_svc.create_scope(req)

    def test_session_increments_across_cycles(self, awm_workspace):
        """Session number should increment correctly across create/complete cycles."""
        from awm.services import scopes as scope_svc
        from awm.models import ScopeCreateRequest, ScopeUpdateRequest
        from awm.db import get_connection

        projects_dir = awm_workspace["projects_dir"]

        # Set up a real bare repo
        bare_dir = projects_dir / "proj-c" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", str(bare_dir)], check=True, capture_output=True)
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "clone", str(bare_dir), tmp], check=True, capture_output=True)
            dummy = os.path.join(tmp, "README.md")
            with open(dummy, "w") as f:
                f.write("init")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmp, "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", tmp, "push"], check=True, capture_output=True)

        # Session 1
        req = ScopeCreateRequest(project="proj-c", scope="cycle-scope")
        resp1 = scope_svc.create_scope(req)
        assert resp1.session == 1

        # Complete session 1
        scope_svc.update_scope("proj-c", "cycle-scope", ScopeUpdateRequest(action="complete"))

        # Session 2
        resp2 = scope_svc.create_scope(req)
        assert resp2.session == 2

        # Complete session 2
        scope_svc.update_scope("proj-c", "cycle-scope", ScopeUpdateRequest(action="complete"))

        # Session 3
        resp3 = scope_svc.create_scope(req)
        assert resp3.session == 3

    def test_list_scopes_includes_session(self, awm_workspace, seeded_scopes):
        """list_scopes should return session numbers."""
        from awm.services import scopes as scope_svc

        result = scope_svc.list_scopes(status="all")
        for s in result.scopes:
            assert s.session >= 1


class TestResolveSession:
    def test_resolve_marks_entry(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Bug in parser",
            issues=["Parser crashes on empty input"],
        )
        entry = sessions.log_session(req)
        assert entry.resolved_at is None

        resolved = sessions.resolve_session(entry.id, "Added empty input guard")
        assert resolved.resolved_at is not None
        assert resolved.resolution == "Added empty input guard"

    def test_resolve_nonexistent_raises(self, awm_workspace):
        with pytest.raises(FileNotFoundError):
            sessions.resolve_session(99999, "n/a")

    def test_re_resolve_updates(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Flaky test",
        )
        entry = sessions.log_session(req)
        sessions.resolve_session(entry.id, "First fix")
        updated = sessions.resolve_session(entry.id, "Better fix")
        assert updated.resolution == "Better fix"


class TestSearchSessions:
    def test_search_by_query(self, awm_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Parser bug fix",
        ))
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Added logging",
        ))
        result = sessions.search_sessions(query="parser")
        assert result.total == 1
        assert result.entries[0].summary == "Parser bug fix"

    def test_search_by_status_open(self, awm_workspace):
        e1 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open issue",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved issue",
        ))
        sessions.resolve_session(e2.id, "Fixed")

        result = sessions.search_sessions(status="open")
        assert result.total == 1
        assert result.entries[0].id == e1.id

    def test_search_by_status_resolved(self, awm_workspace):
        e1 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open issue",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved issue",
        ))
        sessions.resolve_session(e2.id, "Fixed")

        result = sessions.search_sessions(status="resolved")
        assert result.total == 1
        assert result.entries[0].id == e2.id

    def test_search_combined_filters(self, awm_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Parser open",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Parser resolved",
        ))
        sessions.resolve_session(e2.id, "Fixed")
        sessions.log_session(SessionLogCreateRequest(
            project="proj-b", scope="scope-1", summary="Parser other project",
        ))

        result = sessions.search_sessions(project="proj-a", query="parser", status="open")
        assert result.total == 1
        assert result.entries[0].summary == "Parser open"

    def test_search_matches_title(self, awm_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Long narrative",
            title="Fix parser crash",
        ))
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Other work",
            title="Add logging",
        ))
        result = sessions.search_sessions(query="parser")
        assert result.total == 1
        assert result.entries[0].title == "Fix parser crash"

    def test_search_returns_previews(self, awm_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Test preview",
        ))
        result = sessions.search_sessions()
        entry = result.entries[0]
        assert hasattr(entry, "id")
        assert hasattr(entry, "summary")
        assert not hasattr(entry, "content")
        assert not hasattr(entry, "resolution")


class TestListSessionsStatus:
    def test_list_status_open(self, awm_workspace):
        e1 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved",
        ))
        sessions.resolve_session(e2.id, "Done")

        result = sessions.list_sessions(status="open")
        assert result.total == 1
        assert result.entries[0].id == e1.id

    def test_list_status_resolved(self, awm_workspace):
        e1 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved",
        ))
        sessions.resolve_session(e2.id, "Done")

        result = sessions.list_sessions(status="resolved")
        assert result.total == 1
        assert result.entries[0].id == e2.id

    def test_list_default_returns_all(self, awm_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved",
        ))
        sessions.resolve_session(e2.id, "Done")

        result = sessions.list_sessions()
        assert result.total == 2
