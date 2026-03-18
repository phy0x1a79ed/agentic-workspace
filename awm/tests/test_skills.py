"""Tests for awm.services.skills — scanning, frontmatter, search, index."""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.services import skills


class TestParseFrontmatter:
    def test_valid_frontmatter(self, sample_skills_dir):
        path = sample_skills_dir / "sops" / "git-workflow.md"
        fm = skills._parse_frontmatter(path)
        assert fm["name"] == "Git Workflow"
        assert fm["type"] == "sop"
        assert "git" in fm["tags"]

    def test_no_frontmatter(self, sample_skills_dir):
        path = sample_skills_dir / "tools" / "plain.md"
        fm = skills._parse_frontmatter(path)
        assert fm == {}

    def test_malformed_frontmatter(self, awm_workspace):
        path = awm_workspace["skills_dir"] / "bad.md"
        path.write_text("---\n: [invalid yaml\n---\nContent\n")
        fm = skills._parse_frontmatter(path)
        assert fm == {}

    def test_missing_file(self, awm_workspace):
        path = awm_workspace["skills_dir"] / "nonexistent.md"
        fm = skills._parse_frontmatter(path)
        assert fm == {}


class TestTypeInference:
    def test_sop_inferred(self, sample_skills_dir):
        path = sample_skills_dir / "sops" / "testing.md"
        info = skills._skill_from_path(path)
        assert info.type == "sop"

    def test_tool_inferred_no_frontmatter(self, sample_skills_dir):
        path = sample_skills_dir / "tools" / "plain.md"
        info = skills._skill_from_path(path)
        assert info.type == "tool"
        assert info.name == "plain"  # falls back to stem


class TestListSkills:
    def test_list_all(self, sample_skills_dir):
        result = skills.list_skills()
        assert result.total >= 3
        names = {s.name for s in result.skills}
        assert "Git Workflow" in names
        assert "Mamba" in names

    def test_excludes_index(self, sample_skills_dir):
        result = skills.list_skills()
        names = {s.name for s in result.skills}
        assert "_index" not in names

    def test_excludes_templates(self, sample_skills_dir):
        result = skills.list_skills()
        paths = {s.file_path for s in result.skills}
        for p in paths:
            assert not p.startswith("templates/")

    def test_filter_by_type(self, sample_skills_dir):
        result = skills.list_skills(type_filter="sop")
        assert result.total >= 2
        for s in result.skills:
            assert s.type == "sop"

    def test_filter_by_tags(self, sample_skills_dir):
        result = skills.list_skills(tags=["git"])
        assert result.total >= 1
        for s in result.skills:
            assert any("git" in t.lower() for t in s.tags)

    def test_empty_when_no_match(self, sample_skills_dir):
        result = skills.list_skills(type_filter="nonexistent")
        assert result.total == 0

    def test_empty_skills_dir(self, awm_workspace, monkeypatch):
        empty = awm_workspace["workspace"] / "empty_skills"
        monkeypatch.setattr("awm.services.skills.SKILLS_DIR", empty)
        result = skills.list_skills()
        assert result.total == 0


class TestSearchSkills:
    def test_search_by_name(self, sample_skills_dir):
        result = skills.search_skills("mamba")
        assert result.total >= 1

    def test_search_by_content(self, sample_skills_dir):
        result = skills.search_skills("feature branches")
        assert result.total >= 1

    def test_search_no_results(self, sample_skills_dir):
        result = skills.search_skills("xyznonexistent123")
        assert result.total == 0

    def test_search_case_insensitive(self, sample_skills_dir):
        result = skills.search_skills("MAMBA")
        assert result.total >= 1


class TestGetSkill:
    def test_get_existing(self, sample_skills_dir):
        result = skills.get_skill("sops/git-workflow.md")
        assert result.skill.name == "Git Workflow"
        assert "feature branches" in result.content.lower()

    def test_get_missing(self, sample_skills_dir):
        with pytest.raises(FileNotFoundError):
            skills.get_skill("sops/nonexistent.md")


class TestRegenerateIndex:
    def test_regenerate_creates_index(self, sample_skills_dir):
        content = skills.regenerate_index()
        assert "# Skills Catalog" in content
        assert "Git Workflow" in content
        index_path = sample_skills_dir / "_index.md"
        assert index_path.exists()
        assert index_path.read_text() == content

    def test_regenerate_includes_templates(self, sample_skills_dir):
        content = skills.regenerate_index()
        assert "Templates" in content
