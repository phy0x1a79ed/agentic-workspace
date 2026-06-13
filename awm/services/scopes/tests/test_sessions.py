"""Tests for awm.scopes.sessions — log, list, get.

Ported from awm/tests/sessions/test_sessions.py.
- ``from awm.models import SessionLogCreateRequest`` → ``from awm.scopes.models import SessionLogCreateRequest``
- ``from awm.services import sessions`` → ``from awm.scopes import sessions``
- ``awm_workspace`` fixture → ``scopes_workspace``
- ``seeded_sessions`` fixture → scopes dist version in conftest
- DB inserts use inline ``project``/``scope`` cols (no agent_id FK)
"""

from __future__ import annotations

import subprocess

import pytest

from awm.scopes.models import SessionLogCreateRequest
from awm.scopes import sessions


pytestmark = [pytest.mark.sessions, pytest.mark.slow]


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
    def test_log_creates_db_entry(self, scopes_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Test session log",
        )
        entry = sessions.log_session(req)
        assert entry.project == "proj-a"
        assert entry.scope == "scope-1"
        assert entry.git_commit is None

        detail = sessions.get_session(entry.id)
        assert "Test session log" in detail.content

    def test_log_with_metadata(self, scopes_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s",
            decisions=["d1"], issues=["i1"],
        )
        entry = sessions.log_session(req)
        assert entry.id is not None

        detail = sessions.get_session(entry.id)
        assert "d1" in detail.content

    def test_log_with_skill_path(self, scopes_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s",
            skill_path="sops/debrief.md",
        )
        entry = sessions.log_session(req)
        assert entry.skill_path == "sops/debrief.md"

        detail = sessions.get_session(entry.id)
        assert detail.entry.skill_path == "sops/debrief.md"
        assert "**Skill:** sops/debrief.md" in detail.content

    def test_log_with_outcome_and_deviations(self, scopes_workspace):
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

    def test_log_with_title(self, scopes_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Narrative summary",
            title="Fix parser bug",
        )
        entry = sessions.log_session(req)
        assert entry.title == "Fix parser bug"

        detail = sessions.get_session(entry.id)
        assert "# Fix parser bug" in detail.content

    def test_log_with_skill_captures_version(self, scopes_workspace, monkeypatch):
        monkeypatch.setattr(
            "awm.scopes.sessions._get_skills_git_hash",
            lambda: "abc123def456",
        )
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="s",
            skill_path="awm/debrief.md",
        )
        entry = sessions.log_session(req)
        assert entry.skill_version == "abc123def456"


class TestSearchSessions:
    def test_all(self, scopes_workspace, seeded_sessions):
        result = sessions.search_sessions(status="all")
        assert result.total == 3

    def test_by_project(self, scopes_workspace, seeded_sessions):
        result = sessions.search_sessions(status="all", project="proj-a")
        assert result.total == 3

    def test_by_scope(self, scopes_workspace, seeded_sessions):
        result = sessions.search_sessions(status="all", project="proj-a", scope="scope-1")
        assert result.total == 2

    def test_with_limit(self, scopes_workspace, seeded_sessions):
        result = sessions.search_sessions(status="all", limit=1)
        assert result.total == 1

    def test_empty(self, scopes_workspace):
        result = sessions.search_sessions(status="all")
        assert result.total == 0

    def test_by_skill_path(self, scopes_workspace):
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
        result = sessions.search_sessions(status="all", skill_path="awm/debrief.md")
        assert result.total == 1
        assert result.entries[0].skill_path == "awm/debrief.md"

    def test_search_by_query(self, scopes_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Parser bug fix",
        ))
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Added logging",
        ))
        result = sessions.search_sessions(query="parser")
        assert result.total == 1
        assert result.entries[0].summary == "Parser bug fix"

    def test_search_by_status_open(self, scopes_workspace):
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

    def test_search_by_status_resolved(self, scopes_workspace):
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

    def test_search_combined_filters(self, scopes_workspace):
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

    def test_search_matches_title(self, scopes_workspace):
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

    def test_search_returns_previews(self, scopes_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Test preview",
        ))
        result = sessions.search_sessions()
        entry = result.entries[0]
        assert hasattr(entry, "id")
        assert hasattr(entry, "summary")
        assert not hasattr(entry, "content")
        assert not hasattr(entry, "resolution")
        assert entry.summary_truncated is False
        assert entry.summary == "Test preview"

    def test_search_truncates_long_summary(self, scopes_workspace):
        long_summary = "A" * 500
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary=long_summary,
        ))
        result = sessions.search_sessions()
        entry = result.entries[0]
        assert entry.summary_truncated is True
        assert len(entry.summary) == 241
        assert entry.summary.endswith("…")
        assert entry.summary[:240] == "A" * 240


class TestGetSession:
    def test_get_existing(self, scopes_workspace, seeded_sessions):
        result = sessions.search_sessions(status="all")
        entry_id = result.entries[0].id
        detail = sessions.get_session(entry_id)
        assert detail.entry.id == entry_id
        assert detail.content

    def test_get_nonexistent(self, scopes_workspace):
        with pytest.raises(FileNotFoundError):
            sessions.get_session(99999)

    def test_get_resolved_includes_resolution(self, scopes_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Bug found",
        )
        entry = sessions.log_session(req)
        sessions.resolve_session(entry.id, "Added input validation")
        detail = sessions.get_session(entry.id)
        assert "## Resolution" in detail.content
        assert "Added input validation" in detail.content


class TestResolveSession:
    def test_resolve_marks_entry(self, scopes_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Bug in parser",
            issues=["Parser crashes on empty input"],
        )
        entry = sessions.log_session(req)
        assert entry.resolved_at is None

        resolved = sessions.resolve_session(entry.id, "Added empty input guard")
        assert resolved.resolved_at is not None
        assert resolved.resolution == "Added empty input guard"

    def test_resolve_nonexistent_raises(self, scopes_workspace):
        with pytest.raises(FileNotFoundError):
            sessions.resolve_session(99999, "n/a")

    def test_re_resolve_updates(self, scopes_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Flaky test",
        )
        entry = sessions.log_session(req)
        sessions.resolve_session(entry.id, "First fix")
        updated = sessions.resolve_session(entry.id, "Better fix")
        assert updated.resolution == "Better fix"


class TestListSessionsStatus:
    def test_list_status_open(self, scopes_workspace):
        e1 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved",
        ))
        sessions.resolve_session(e2.id, "Done")

        result = sessions.search_sessions(status="open")
        assert result.total == 1
        assert result.entries[0].id == e1.id

    def test_list_status_resolved(self, scopes_workspace):
        e1 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved",
        ))
        sessions.resolve_session(e2.id, "Done")

        result = sessions.search_sessions(status="resolved")
        assert result.total == 1
        assert result.entries[0].id == e2.id

    def test_list_default_excludes_resolved(self, scopes_workspace):
        sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Open",
        ))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="proj-a", scope="scope-1", summary="Resolved",
        ))
        sessions.resolve_session(e2.id, "Done")

        result = sessions.search_sessions()
        assert result.total == 1
        assert result.entries[0].id != e2.id
