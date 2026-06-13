"""Tests for the skills-dist Pydantic models (split out of the monolith's
``unit/test_models.py``)."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.misc, pytest.mark.smoke]

from awm.skills.models import SkillInfo


class TestSkillInfo:
    def test_defaults(self):
        s = SkillInfo(name="test", type="awm", file_path="awm/test.md")
        assert s.tags == []
        assert s.description == ""
