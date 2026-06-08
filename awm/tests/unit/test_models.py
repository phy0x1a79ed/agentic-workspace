"""Tests for awm.models — Pydantic validation."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import pytest
from pydantic import ValidationError

from awm.models import (
    ScopeCreateRequest,
    ScopeUpdateRequest,
    SessionLogCreateRequest,
    SkillInfo,
    StatusResponse,
    ScopeInfo,
    SessionLogEntry,
)


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
        assert r.active_scopes == 0
