"""Tests for awm.services.sessions — log, list, get, migrate."""

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
    def test_log_creates_db_entry(self, awm_workspace):
        """log_session creates a DB row with content, no file I/O."""
        req = SessionLogCreateRequest(
            project="proj-a", task="task-1", summary="Test session log",
        )
        entry = sessions.log_session(req)
        assert entry.project == "proj-a"
        assert entry.task == "task-1"
        assert entry.git_commit is None

        # Verify content in DB
        detail = sessions.get_session(entry.id)
        assert "Test session log" in detail.content

    def test_log_with_metadata(self, awm_workspace):
        req = SessionLogCreateRequest(
            project="proj-a", task="task-1", summary="s",
            decisions=["d1"], issues=["i1"],
        )
        entry = sessions.log_session(req)
        assert entry.id is not None

        detail = sessions.get_session(entry.id)
        assert "d1" in detail.content


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
        result = sessions.list_sessions()
        entry_id = result.entries[0].id
        detail = sessions.get_session(entry_id)
        assert detail.entry.id == entry_id
        assert detail.content  # content comes from DB

    def test_get_nonexistent(self, awm_workspace):
        with pytest.raises(FileNotFoundError):
            sessions.get_session(99999)


class TestMigrateExperiences:
    def test_migrate_structured_entry(self, awm_workspace):
        main_dir = awm_workspace["main_dir"]
        ws = main_dir / "proj-x" / "tasks" / "task-x"
        ws.mkdir(parents=True)
        (ws / "experiences.md").write_text(
            "# Experiences -- proj-x/task-x\n\n## Log\n\n"
            "\n**Date:** 2024-06-15\n\n"
            "**Task:** proj-x/task-x\n\n"
            "**Agent:** agent-1\n\n"
            "## Session Summary\n\n"
            "Did some work\n\n"
            "---\n"
        )

        stats = sessions.migrate_experiences()
        assert stats["imported"] == 1
        assert stats["files_processed"] == 1

        result = sessions.list_sessions(project="proj-x")
        assert result.total == 1
        assert "Did some work" in result.entries[0].summary

    def test_migrate_free_form_entry(self, awm_workspace):
        main_dir = awm_workspace["main_dir"]
        ws = main_dir / "proj-y" / "tasks" / "task-y"
        ws.mkdir(parents=True)
        (ws / "experiences.md").write_text(
            "# Experiences -- proj-y/task-y\n\n## Log\n\n"
            "Some free-form notes about this task\n"
            "that span multiple lines.\n"
        )

        stats = sessions.migrate_experiences()
        assert stats["imported"] == 1

    def test_migrate_empty_file(self, awm_workspace):
        main_dir = awm_workspace["main_dir"]
        ws = main_dir / "proj-z" / "tasks" / "task-z"
        ws.mkdir(parents=True)
        (ws / "experiences.md").write_text(
            "# Experiences -- proj-z/task-z\n\n## Log\n\n"
        )

        stats = sessions.migrate_experiences()
        assert stats["imported"] == 0

    def test_migrate_dedup(self, awm_workspace):
        """Second migration should skip already-imported entries."""
        main_dir = awm_workspace["main_dir"]
        ws = main_dir / "proj-d" / "tasks" / "task-d"
        ws.mkdir(parents=True)
        (ws / "experiences.md").write_text(
            "# Experiences -- proj-d/task-d\n\n## Log\n\n"
            "\n**Date:** 2024-06-15\n\n"
            "**Task:** proj-d/task-d\n\n"
            "**Agent:** agent-1\n\n"
            "## Session Summary\n\n"
            "First entry\n\n"
            "---\n"
        )

        stats1 = sessions.migrate_experiences()
        assert stats1["imported"] == 1

        stats2 = sessions.migrate_experiences()
        assert stats2["skipped"] == 1
        assert stats2["imported"] == 0


# ---------------------------------------------------------------------------
# Task session lifecycle
# ---------------------------------------------------------------------------

class TestTaskSessions:
    """Test session numbering when re-creating completed tasks."""

    def test_create_on_completed_task_creates_session_2(self, awm_workspace, seeded_tasks):
        """Re-creating a completed task should increment the session number."""
        from awm.services import tasks as task_svc
        from awm.models import TaskCreateRequest

        repos_dir = awm_workspace["repos_dir"]

        # task-2 is completed in seeded_tasks — set up bare repo for it
        bare_dir = repos_dir / "proj-a" / ".bare"
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

        req = TaskCreateRequest(project="proj-a", task="task-2")
        resp = task_svc.create_task(req)
        assert resp.session == 2
        assert resp.status == "active"

    def test_create_on_active_task_raises(self, awm_workspace, seeded_tasks):
        """Creating a task that already has an active session should fail."""
        from awm.services import tasks as task_svc
        from awm.models import TaskCreateRequest

        req = TaskCreateRequest(project="proj-a", task="task-1")
        with pytest.raises(FileExistsError, match="active session"):
            task_svc.create_task(req)

    def test_session_increments_across_cycles(self, awm_workspace):
        """Session number should increment correctly across create/complete cycles."""
        from awm.services import tasks as task_svc
        from awm.models import TaskCreateRequest, TaskUpdateRequest
        from awm.db import get_connection

        repos_dir = awm_workspace["repos_dir"]

        # Set up a real bare repo
        bare_dir = repos_dir / "proj-c" / ".bare"
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
        req = TaskCreateRequest(project="proj-c", task="cycle-task")
        resp1 = task_svc.create_task(req)
        assert resp1.session == 1

        # Complete session 1
        task_svc.update_task("proj-c", "cycle-task", TaskUpdateRequest(action="complete"))

        # Session 2
        resp2 = task_svc.create_task(req)
        assert resp2.session == 2

        # Complete session 2
        task_svc.update_task("proj-c", "cycle-task", TaskUpdateRequest(action="complete"))

        # Session 3
        resp3 = task_svc.create_task(req)
        assert resp3.session == 3

    def test_list_tasks_includes_session(self, awm_workspace, seeded_tasks):
        """list_tasks should return session numbers."""
        from awm.services import tasks as task_svc

        result = task_svc.list_tasks(status="all")
        for t in result.tasks:
            assert t.session >= 1
