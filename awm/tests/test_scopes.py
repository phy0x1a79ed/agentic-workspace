"""Tests for awm.services.scopes — list (DB-only) + mocked create/update/delete."""

from __future__ import annotations

import subprocess

import pytest

from awm.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.services import scopes


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
