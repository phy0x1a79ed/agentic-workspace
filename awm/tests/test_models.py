"""Tests for awm.models — Pydantic validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awm.models import (
    LockAcquireRequest,
    LockReleaseRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
    SessionLogCreateRequest,
    SkillInfo,
    StatusResponse,
    LockInfo,
    TaskInfo,
    SessionLogEntry,
)


class TestLockAcquireRequest:
    def test_valid_exclusive(self):
        req = LockAcquireRequest(resource_path="tasks/p/t", holder_id="agent-1")
        assert req.lock_type == "exclusive"
        assert req.holder_pid is None

    def test_valid_shared(self):
        req = LockAcquireRequest(
            resource_path="data/file.csv", holder_id="agent-2",
            lock_type="shared", holder_pid=1234,
        )
        assert req.lock_type == "shared"
        assert req.holder_pid == 1234

    def test_invalid_lock_type(self):
        with pytest.raises(ValidationError, match="lock_type"):
            LockAcquireRequest(
                resource_path="x", holder_id="a", lock_type="invalid"
            )

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            LockAcquireRequest()


class TestTaskCreateRequest:
    def test_defaults(self):
        req = TaskCreateRequest(project="proj", task="t1")
        assert req.from_branch is None

    def test_with_branch(self):
        req = TaskCreateRequest(project="proj", task="t1", from_branch="dev")
        assert req.from_branch == "dev"


class TestTaskUpdateRequest:
    def test_valid(self):
        req = TaskUpdateRequest(action="complete")
        assert req.merge is False

    def test_with_merge(self):
        req = TaskUpdateRequest(action="complete", merge=True)
        assert req.merge is True


class TestSessionLogCreateRequest:
    def test_minimal(self):
        req = SessionLogCreateRequest(project="p", task="t", summary="Did stuff")
        assert req.agent_id == "unknown"
        assert req.decisions == []
        assert req.issues == []
        assert req.next_steps == []

    def test_full(self):
        req = SessionLogCreateRequest(
            project="p", task="t", summary="s",
            decisions=["d1"], issues=["i1"], next_steps=["n1"],
            agent_id="agent-x",
        )
        assert len(req.decisions) == 1
        assert req.agent_id == "agent-x"


class TestSkillInfo:
    def test_defaults(self):
        s = SkillInfo(name="test", type="sop", file_path="sops/test.md")
        assert s.tags == []
        assert s.description == ""


class TestStatusResponse:
    def test_defaults(self):
        r = StatusResponse(workspace_root="/tmp")
        assert r.status == "ok"
        assert r.active_locks == 0


class TestLockInfo:
    def test_all_fields(self):
        info = LockInfo(
            id=1, resource_path="x", holder_id="a",
            holder_pid=None, lock_type="exclusive",
            acquired_at="2024-01-01", heartbeat_at="2024-01-01",
            metadata=None,
        )
        assert info.id == 1
