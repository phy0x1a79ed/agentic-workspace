"""Tests for awm.services.sessions — log, list, get, reflect."""

from __future__ import annotations

import subprocess

import pytest

from awm.models import SessionLogCreateRequest
from awm.services import sessions


class TestFormatEntry:
    def test_minimal_entry(self):
        req = SessionLogCreateRequest(project="p", task="t", summary="Did things")
        text = sessions._format_entry(req, "2024-06-15T12:00:00+00:00")
        assert "**Date:** 2024-06-15" in text
        assert "**Task:** p/t" in text
        assert "Did things" in text

    def test_full_entry(self):
        req = SessionLogCreateRequest(
            project="p", task="t", summary="Summary",
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
        req = SessionLogCreateRequest(project="p", task="t", summary="s")
        text = sessions._format_entry(req, "2024-01-01T00:00:00+00:00")
        assert text.strip().endswith("---")


class TestLogSession:
    def test_log_creates_file_and_db_entry(self, awm_workspace, seeded_tasks):
        """log_session should append to experiences.md and insert a DB row."""
        req = SessionLogCreateRequest(
            project="proj-a", task="task-1", summary="Test session log",
        )
        entry = sessions.log_session(req)
        assert entry.project == "proj-a"
        assert entry.task == "task-1"
        assert entry.git_commit is None  # no git commits in new layout

        # Check file was appended in workspace (main/)
        exp_file = awm_workspace["main_dir"] / "proj-a" / "task-1" / "experiences.md"
        content = exp_file.read_text()
        assert "Test session log" in content

    def test_log_missing_workspace(self, awm_workspace):
        req = SessionLogCreateRequest(project="noproject", task="notask", summary="s")
        with pytest.raises(FileNotFoundError):
            sessions.log_session(req)

    def test_log_with_metadata(self, awm_workspace, seeded_tasks):
        req = SessionLogCreateRequest(
            project="proj-a", task="task-1", summary="s",
            decisions=["d1"], issues=["i1"],
        )
        entry = sessions.log_session(req)
        assert entry.id is not None


class TestListSessions:
    def test_list_all(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions()
        assert result.total == 3

    def test_list_by_project(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions(project="proj-a")
        assert result.total == 3

    def test_list_by_task(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions(project="proj-a", task="task-1")
        assert result.total == 2

    def test_list_with_limit(self, awm_workspace, seeded_sessions):
        result = sessions.list_sessions(limit=1)
        assert result.total == 1

    def test_list_empty(self, awm_workspace):
        result = sessions.list_sessions()
        assert result.total == 0


class TestGetSession:
    def test_get_existing(self, awm_workspace, seeded_sessions):
        # First get the list to find an ID
        result = sessions.list_sessions()
        entry_id = result.entries[0].id
        detail = sessions.get_session(entry_id)
        assert detail.entry.id == entry_id

    def test_get_nonexistent(self, awm_workspace):
        with pytest.raises(FileNotFoundError):
            sessions.get_session(99999)


class TestReflect:
    def test_reflect_all(self, awm_workspace, seeded_sessions):
        result = sessions.reflect()
        assert result.total == 3

    def test_reflect_by_project(self, awm_workspace, seeded_sessions):
        result = sessions.reflect(project="proj-a")
        assert result.total >= 1

    def test_reflect_with_query(self, awm_workspace, seeded_sessions):
        result = sessions.reflect(query="pipeline")
        assert result.total >= 1
        assert any("pipeline" in e.summary.lower() for e in result.entries)

    def test_reflect_no_match(self, awm_workspace, seeded_sessions):
        result = sessions.reflect(query="zzz_nonexistent_zzz")
        assert result.total == 0
