"""Tests for awm.models — Pydantic validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awm.models import (
    LockAcquireRequest,
    LockReleaseRequest,
    ScopeCreateRequest,
    ScopeUpdateRequest,
    SessionLogCreateRequest,
    SkillInfo,
    StatusResponse,
    LockInfo,
    ScopeInfo,
    SessionLogEntry,
)


class TestLockAcquireRequest:
    def test_valid_exclusive(self):
        req = LockAcquireRequest(resource_path="projects/p/s", holder_id="agent-1")
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


class TestScopeCreateRequest:
    def test_defaults(self):
        req = ScopeCreateRequest(project="proj", scope="s1")
        assert req.from_branch is None

    def test_with_branch(self):
        req = ScopeCreateRequest(project="proj", scope="s1", from_branch="develop")
        assert req.from_branch == "develop"


class TestScopeUpdateRequest:
    def test_valid(self):
        req = ScopeUpdateRequest(action="complete")
        assert req.merge is False

    def test_with_merge(self):
        req = ScopeUpdateRequest(action="complete", merge=True)
        assert req.merge is True


class TestSessionLogCreateRequest:
    def test_minimal(self):
        req = SessionLogCreateRequest(project="p", scope="t", summary="Did stuff")
        assert req.agent_id == "unknown"
        assert req.decisions == []
        assert req.issues == []
        assert req.next_steps == []

    def test_full(self):
        req = SessionLogCreateRequest(
            project="p", scope="t", summary="s",
            decisions=["d1"], issues=["i1"], next_steps=["n1"],
            agent_id="agent-x",
        )
        assert len(req.decisions) == 1
        assert req.agent_id == "agent-x"


class TestSkillInfo:
    def test_defaults(self):
        s = SkillInfo(name="test", type="awm", file_path="awm/test.md")
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
