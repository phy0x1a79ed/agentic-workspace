"""Workspace unit lifecycle: create / retain / destroy / resolve + pre-readings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.workspace, pytest.mark.smoke]


@pytest.fixture()
def units(awm_workspace):
    from awm.workspace import units as u
    return u


class TestCreate:
    def test_lays_out_unit_dirs_and_brief(self, units):
        res = units.create(unit_slug="u1", context_md="# brief\nhello")
        path = Path(res["path"])
        assert path.is_dir()
        # The brief is CLAUDE.md so the claude harness auto-loads it.
        assert (path / "CLAUDE.md").read_text() == "# brief\nhello"
        assert (path / "inputs").is_dir()
        assert (path / "deliverable").is_dir()
        assert (path / "scratch").is_dir()
        assert res["state"] == "active"
        # No project key in the return.
        assert "project" not in res

    def test_unit_lives_under_tasks_dir_no_project_segment(self, units, awm_workspace):
        res = units.create(unit_slug="u1b", context_md="")
        # The unit lands at tasks/<slug>/ — no project directory segment.
        assert Path(res["path"]) == awm_workspace["tasks_dir"] / "u1b"

    def test_scaffolds_awm_dir_with_no_data_symlink(self, units):
        res = units.create(unit_slug="u1c", context_md="")
        awm_dir = Path(res["path"]) / ".awm"
        assert awm_dir.is_dir()
        # A task has no single project → no shared-data dir → no .awm/data link.
        assert not (awm_dir / "data").exists()
        assert not (awm_dir / "data").is_symlink()
        assert not (awm_dir / "skills").exists()

    def test_links_repos_to_scopes(self, units):
        res = units.create(
            unit_slug="u1d", context_md="",
            repos=[{"name": "core", "project": "awm", "scope": "svc-agents"}],
        )
        assert res["repos"] == ["core"]
        link = Path(res["path"]) / "repos" / "core"
        assert link.is_symlink()
        # Relative target; from tasks/<slug>/repos/<name> it resolves to the
        # workspace-root scope worktree projects/<rproject>/<rscope>.
        assert os.readlink(link) == "../../../projects/awm/svc-agents"

    def test_repo_symlink_resolves_to_scope_worktree(self, units, awm_workspace):
        res = units.create(
            unit_slug="u1e", context_md="",
            repos=[{"name": "core", "project": "awm", "scope": "svc-agents"}],
        )
        link = Path(res["path"]) / "repos" / "core"
        # The link resolves (lexically) to <workspace_root>/projects/awm/svc-agents.
        resolved = (link.parent / Path(os.readlink(link))).resolve()
        expected = (awm_workspace["workspace"] / "projects" / "awm" / "svc-agents").resolve()
        assert resolved == expected

    def test_materializes_inline_prereadings_read_only(self, units):
        res = units.create(
            unit_slug="u2", context_md="",
            prereadings=[{"name": "spec.md", "content": "the spec"}],
        )
        assert res["inputs"] == ["spec.md"]
        f = Path(res["path"]) / "inputs" / "spec.md"
        assert f.read_text() == "the spec"
        # Read-only: write bit stripped (best-effort).
        assert (f.stat().st_mode & 0o222) == 0

    def test_materializes_path_prereadings_by_copy(self, units, tmp_path):
        src = tmp_path / "source.txt"
        src.write_text("from disk")
        res = units.create(
            unit_slug="u3", context_md="",
            prereadings=[{"name": "copied.txt", "path": str(src)}],
        )
        assert (Path(res["path"]) / "inputs" / "copied.txt").read_text() == "from disk"

    def test_idempotent_rewrites_context_keeps_deliverables(self, units):
        res1 = units.create(unit_slug="u4", context_md="v1")
        deliverable = Path(res1["path"]) / "deliverable" / "out"
        deliverable.mkdir(parents=True)
        (deliverable / "payload").write_text("work")
        # Re-create: CONTEXT rewritten, prior deliverable survives.
        res2 = units.create(unit_slug="u4", context_md="v2")
        assert (Path(res2["path"]) / "CLAUDE.md").read_text() == "v2"
        assert (deliverable / "payload").read_text() == "work"


class TestRetain:
    def test_marks_idle_keeps_contents(self, units):
        res = units.create(unit_slug="r1", context_md="x")
        out = units.retain(unit_slug="r1")
        assert out["retained"] is True
        assert out["state"] == "idle"
        assert (Path(res["path"]) / "CLAUDE.md").exists()
        # Resolve reflects the idle state.
        assert units.resolve(unit_slug="r1")["state"] == "idle"

    def test_unknown_unit_is_safe(self, units):
        out = units.retain(unit_slug="ghost")
        assert out["retained"] is False
        assert out["state"] == "unknown"


class TestDestroy:
    def test_removes_dir_and_row(self, units):
        res = units.create(unit_slug="d1", context_md="x")
        path = Path(res["path"])
        out = units.destroy(unit_slug="d1")
        assert out["destroyed"] is True
        assert not path.exists()
        assert units.resolve(unit_slug="d1")["state"] == "unknown"

    def test_idempotent_on_missing(self, units):
        # Destroying a never-created unit doesn't raise.
        out = units.destroy(unit_slug="never")
        assert out["destroyed"] is False


class TestResolve:
    def test_unknown_reports_path_without_row(self, units):
        out = units.resolve(unit_slug="nope")
        assert out["state"] == "unknown"
        assert out["exists"] is False
        assert out["path"].endswith("/nope")

    def test_active_after_create(self, units):
        units.create(unit_slug="ok", context_md="")
        out = units.resolve(unit_slug="ok")
        assert out["state"] == "active"
        assert out["exists"] is True
