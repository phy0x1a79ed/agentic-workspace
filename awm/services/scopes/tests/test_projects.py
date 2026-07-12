"""Tests for awm.scopes.projects — clone-mode scaffolding + credential errors.

Rewritten after ``_find_template`` (dead code) was removed and
``create_project`` gained scope-style ``.awm/`` scaffolding (inbox bug report
2026-07-01). Uses a local seed repo as the clone source so the clone path is
exercised hermetically (no network, no ``gh``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

import awm.scopes.projects as projects_mod
from awm.scopes.projects import _clone_bare, create_project
from awm.scopes.models import ProjectCreateRequest


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def seed_repo(tmp_path: Path) -> Path:
    """A local non-bare repo with one commit on `main` — a clone source."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("hello\n")
    _git(seed, "add", "README")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    return seed


class TestCreateProjectClone:
    def test_clone_scaffolds_worktree_and_db_row(self, scopes_workspace, seed_repo):
        result = create_project(
            ProjectCreateRequest(name="cloneproj", clone_url=str(seed_repo))
        )
        assert result.mode == "clone"

        projects_dir = scopes_workspace["projects_dir"]
        bare = projects_dir / "cloneproj" / ".bare"
        assert (bare / "HEAD").exists(), "bare repo not created"

        # Default-branch worktree exists and is checked out to `main`.
        worktree = projects_dir / "cloneproj" / "main"
        assert worktree.is_dir()
        head = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert head == "main"

        # .awm/ scaffolded exactly like a scope.
        awm_dir = worktree / ".awm"
        assert (awm_dir / "context.md").exists()
        assert (awm_dir / "history.md").exists()
        assert (awm_dir / "artifacts.md").exists()
        assert (awm_dir / "data").is_symlink()
        assert (awm_dir / "skills").is_symlink()

        # Project is visible without a scope_repair.
        from awm.scopes import projects as p
        listing = p.search_projects()
        assert any(x.name == "cloneproj" for x in listing.projects)

    def test_no_agents_md_written(self, scopes_workspace, seed_repo):
        """create_project must not dirty the cloned worktree with a tracked file."""
        create_project(
            ProjectCreateRequest(name="cleanproj", clone_url=str(seed_repo))
        )
        worktree = scopes_workspace["projects_dir"] / "cleanproj" / "main"
        status = _git(worktree, "status", "--porcelain").stdout.strip()
        assert status == "", f"worktree unexpectedly dirty:\n{status}"
        assert not (worktree / "AGENTS.md").exists()


class TestCloneBare:
    def test_auth_failure_gives_actionable_error(self, tmp_path, monkeypatch):
        """A 'could not read Username' stderr yields a RuntimeError naming the fix."""
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, returncode=128,
                stderr="fatal: could not read Username for 'https://github.com': "
                       "No such device or address",
            )
        monkeypatch.setattr(projects_mod, "_run", fake_run)

        with pytest.raises(RuntimeError) as ei:
            _clone_bare("https://github.com/owner/private.git", tmp_path / ".bare")
        msg = str(ei.value)
        assert "gh auth status" in msg
        assert "repo" in msg

    def test_generic_failure_reports_exit_and_stderr(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, returncode=128,
                stderr="fatal: repository 'x' does not exist",
            )
        monkeypatch.setattr(projects_mod, "_run", fake_run)

        with pytest.raises(RuntimeError) as ei:
            _clone_bare("x", tmp_path / ".bare")
        msg = str(ei.value)
        assert "SSH_AUTH_SOCK" not in msg
        assert "does not exist" in msg

    def test_success_is_silent(self, scopes_workspace, seed_repo):
        bare = scopes_workspace["projects_dir"] / "ok" / ".bare"
        bare.parent.mkdir(parents=True, exist_ok=True)
        _clone_bare(str(seed_repo), bare)
        assert (bare / "HEAD").exists()
