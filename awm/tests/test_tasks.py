"""Tests for awm.services.tasks — list (DB-only) + mocked create/update/delete."""

from __future__ import annotations

import subprocess

import pytest

from awm.models import TaskCreateRequest, TaskUpdateRequest
from awm.services import tasks


class TestListTasks:
    def test_list_empty(self, awm_workspace):
        result = tasks.list_tasks()
        assert result.total == 0
        assert result.tasks == []

    def test_list_all(self, awm_workspace, seeded_tasks):
        result = tasks.list_tasks()
        assert result.total == 3

    def test_list_by_status(self, awm_workspace, seeded_tasks):
        result = tasks.list_tasks(status="active")
        assert result.total == 1
        assert result.tasks[0].status == "active"

    def test_list_by_project(self, awm_workspace, seeded_tasks):
        result = tasks.list_tasks(project="proj-a")
        assert result.total == 2

    def test_list_combined_filters(self, awm_workspace, seeded_tasks):
        result = tasks.list_tasks(status="completed", project="proj-b")
        assert result.total == 1
        assert result.tasks[0].task == "task-3"

    def test_list_status_all(self, awm_workspace, seeded_tasks):
        result = tasks.list_tasks(status="all")
        assert result.total == 3

    def test_list_includes_repo_path(self, awm_workspace, seeded_tasks):
        result = tasks.list_tasks(status="active")
        assert result.tasks[0].repo_path is not None
        assert "repos/" in result.tasks[0].repo_path


class TestCreateTask:
    def test_create_missing_project(self, awm_workspace):
        req = TaskCreateRequest(project="nope", task="t1")
        with pytest.raises(FileNotFoundError, match="not found"):
            tasks.create_task(req)


class TestUpdateTask:
    def test_complete_task(self, awm_workspace, seeded_tasks, monkeypatch):
        monkeypatch.setattr(
            "awm.git_utils.run_git",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=1),
        )
        # Fake a bare dir so the check passes
        bare_dir = awm_workspace["repos_dir"] / "proj-a" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)

        req = TaskUpdateRequest(action="complete")
        result = tasks.update_task("proj-a", "task-1", req)
        assert result.status == "completed"

        # Verify DB was updated
        db_result = tasks.list_tasks(status="completed")
        completed = [t.task for t in db_result.tasks]
        assert "task-1" in completed

    def test_update_missing_project(self, awm_workspace):
        req = TaskUpdateRequest(action="complete")
        with pytest.raises(FileNotFoundError):
            tasks.update_task("nope", "nope", req)

    def test_invalid_action_rejected(self, awm_workspace, seeded_tasks):
        with pytest.raises(Exception):
            TaskUpdateRequest(action="pause")


class TestDeleteTask:
    def test_delete_task(self, awm_workspace, seeded_tasks, monkeypatch):
        monkeypatch.setattr(
            "awm.git_utils.run_git",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode=0),
        )
        bare_dir = awm_workspace["repos_dir"] / "proj-a" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)

        result = tasks.delete_task("proj-a", "task-1")
        assert result.status == "deleted"

        # Verify DB was updated
        db_result = tasks.list_tasks(status="deleted")
        deleted = [t.task for t in db_result.tasks]
        assert "task-1" in deleted

    def test_delete_missing_task(self, awm_workspace):
        bare_dir = awm_workspace["repos_dir"] / "nope" / ".bare"
        bare_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError):
            tasks.delete_task("nope", "nope")
