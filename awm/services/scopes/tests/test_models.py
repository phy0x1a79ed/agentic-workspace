"""Tests for the scopes-dist Pydantic models (split out of the monolith's
``unit/test_models.py``; the gateway envelope StatusResponse moved to the
gateway dist and SkillInfo to the skills dist)."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.smoke]

# NOTE: SessionLogCreateRequest was removed from the scopes models by the
# rooms→scope-channel collapse (T4) — session logs are now ``scope_posts``
# rows, not a dedicated request model. Its test class was dropped here.
from awm.scopes.models import (
    ScopeCreateRequest,
    ScopeUpdateRequest,
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
