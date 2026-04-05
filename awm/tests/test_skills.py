"""Tests for awm.services.skills — scanning, frontmatter, search, index."""

from __future__ import annotations

from pathlib import Path

import pytest

from awm.services import skills


class TestParseFrontmatter:
    def test_valid_frontmatter(self, sample_skills_dir):
        path = sample_skills_dir / "tools" / "git.md"
        fm = skills._parse_frontmatter(path)
        assert fm["name"] == "git"
        assert fm["type"] == "tool"
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
    def test_awm_inferred(self, sample_skills_dir):
        path = sample_skills_dir / "awm" / "debrief.md"
        info = skills._skill_from_path(path)
        assert info.type == "awm"

    def test_tool_inferred_no_frontmatter(self, sample_skills_dir):
        path = sample_skills_dir / "tools" / "plain.md"
        info = skills._skill_from_path(path)
        # Type is inferred from the top-level subdirectory name, verbatim.
        assert info.type == "tools"
        assert info.name == "plain"  # falls back to stem


class TestListSkills:
    def test_list_all(self, sample_skills_dir):
        result = skills.list_skills()
        assert result.total >= 3
        names = {s.name for s in result.skills}
        assert "git" in names
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
        result = skills.list_skills(type_filter="awm")
        assert result.total >= 2
        for s in result.skills:
            assert s.type == "awm"

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
        result = skills.get_skill("tools/git.md")
        assert result.skill.name == "git"
        assert "feature branches" in result.content.lower()

    def test_get_missing(self, sample_skills_dir):
        with pytest.raises(FileNotFoundError):
            skills.get_skill("tools/nonexistent.md")


class TestSyncSkills:
    """Tests for sync_skills with embedding stubbed out.

    `index_skill` depends on sentence-transformers and sqlite-vec — heavy optional
    deps that may not be available in the test environment, and which would slow
    tests down even when they are. Patching it to a no-op isolates the sync
    reconciliation logic (fingerprint, upsert count, prune) from the embedding
    backend.
    """

    @pytest.fixture(autouse=True)
    def _stub_index_skill(self, monkeypatch):
        monkeypatch.setattr(
            "awm.services.embeddings.index_skill",
            lambda path: None,
        )

    def test_first_call_indexes_and_prunes(self, sample_skills_dir, db_conn):
        # Seed a stale skill embedding whose file does not exist.
        db_conn.execute(
            "INSERT INTO embeddings (source_type, source_id, chunk_text, embedding, updated_at) "
            "VALUES ('skill', 'sops/ghost.md', 'ghost', X'00', datetime('now'))"
        )
        db_conn.commit()

        result = skills.sync_skills()
        assert result["skipped"] is False
        assert result["indexed"] >= 1
        assert result["pruned"] == 1

        # Ghost row is gone.
        row = db_conn.execute(
            "SELECT 1 FROM embeddings WHERE source_type='skill' AND source_id='sops/ghost.md'"
        ).fetchone()
        assert row is None

    def test_second_call_is_lazy_noop(self, sample_skills_dir):
        first = skills.sync_skills()
        assert first["skipped"] is False
        second = skills.sync_skills()
        assert second["skipped"] is True
        assert second["reason"] == "fingerprint_unchanged"

    def test_edit_invalidates_fingerprint(self, sample_skills_dir):
        skills.sync_skills()
        # Touch a skill so its mtime bumps.
        target = sample_skills_dir / "tools" / "git.md"
        text = target.read_text()
        target.write_text(text + "\n<!-- edit -->\n")
        result = skills.sync_skills()
        assert result["skipped"] is False

    def test_force_bypasses_fingerprint(self, sample_skills_dir):
        skills.sync_skills()
        result = skills.sync_skills(force=True)
        assert result["skipped"] is False


class TestFindByName:
    def test_find_existing(self, sample_skills_dir):
        hit = skills.find_by_name("debrief")
        assert hit is not None
        assert hit.file_path == "awm/debrief.md"

    def test_find_missing(self, sample_skills_dir):
        assert skills.find_by_name("nonexistent-skill") is None

    def test_find_is_layout_independent(self, sample_skills_dir):
        """Renaming the directory a skill lives in should not break find_by_name."""
        # Move awm/debrief.md to a differently-named directory, stripping the
        # frontmatter `type:` field so inference kicks in from the new dir name.
        new_dir = sample_skills_dir / "workflows"
        new_dir.mkdir()
        src = sample_skills_dir / "awm" / "debrief.md"
        (new_dir / "debrief.md").write_text(
            "---\nname: debrief\ntags: [session]\n"
            "description: End-of-session debrief\n---\n\n# Debrief\n\nLog the session.\n"
        )
        src.unlink()
        hit = skills.find_by_name("debrief")
        assert hit is not None
        assert hit.file_path == "workflows/debrief.md"
        assert hit.type == "workflows"  # type auto-inferred from the new dir name
