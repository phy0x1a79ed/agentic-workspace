"""Real-git tests for the parts of nesting that are not name validation.

Two of these guard destructive paths. Worktree *enumeration* feeds ``data_gc``'s
keep-set, so a worktree missing from it is a worktree whose pinned cache objects
get collected — and the one-level glob this replaced could not see a nested
scope. The branch pre-check turns git's directory/file ref conflict into a
message that names both branches, which matters because the default naming
(``feat/<scope>``) collides with an existing ``feat/fabfos`` the moment anyone
asks for ``fabfos/dev``.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

import subprocess
from pathlib import Path

import pytest

from awm.scopes.scopes import _assert_branch_available, _project_worktrees


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    """A project bare repo with a flat scope and a nested one, with
    ``PROJECTS_DIR`` pointed at the containing directory."""
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "metasmith"
    proj.mkdir(parents=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("x")
    _git(seed, "add", "README")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    bare = proj / ".bare"
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(bare)],
                   check=True, capture_output=True)
    _git(bare, "worktree", "add", "-b", "feat/monorepo", str(proj / "monorepo"), "main")
    _git(bare, "worktree", "add", "-b", "fabfos/dev", str(proj / "fabfos" / "dev"), "main")
    _git(bare, "worktree", "add", "-b", "engine/dev", str(proj / "engine" / "dev"), "main")

    import awm.scopes.scopes as mod
    monkeypatch.setattr(mod, "PROJECTS_DIR", projects_dir)
    return proj


class TestProjectWorktrees:
    def test_lists_flat_and_nested(self, project):
        found = {p.resolve() for p in _project_worktrees("metasmith")}
        assert found == {
            (project / "monorepo").resolve(),
            (project / "fabfos" / "dev").resolve(),
            (project / "engine" / "dev").resolve(),
        }

    def test_excludes_the_bare_repo_itself(self, project):
        assert (project / ".bare").resolve() not in {
            p.resolve() for p in _project_worktrees("metasmith")
        }

    def test_excludes_an_unrelated_checkout_living_under_the_project(self, project):
        """A scope that clones a dependency must not have that clone counted as
        a scope — the reason the old code refused to recurse."""
        vendored = project / "monorepo" / "vendor" / "upstream"
        vendored.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(vendored)],
                       check=True, capture_output=True)
        found = {p.resolve() for p in _project_worktrees("metasmith")}
        assert vendored.resolve() not in found

    def test_raises_rather_than_returning_a_short_list(self, tmp_path, monkeypatch):
        """An empty list would read as 'keep nothing' to data_gc."""
        import awm.scopes.scopes as mod
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path / "nowhere")
        with pytest.raises(FileNotFoundError):
            _project_worktrees("metasmith")


class TestAssertBranchAvailable:
    def test_accepts_a_free_nested_name(self, project):
        _assert_branch_available(project / ".bare", "libraries/mono")

    def test_refuses_a_branch_under_an_existing_one(self, project):
        _git(project / ".bare", "branch", "feat/fabfos", "main")
        with pytest.raises(ValueError, match="feat/fabfos"):
            _assert_branch_available(project / ".bare", "feat/fabfos/dev")

    def test_refuses_a_branch_above_existing_ones(self, project):
        with pytest.raises(ValueError, match="fabfos/dev"):
            _assert_branch_available(project / ".bare", "fabfos")

    def test_refuses_a_malformed_ref(self, project):
        with pytest.raises(ValueError):
            _assert_branch_available(project / ".bare", "bad..name")

    def test_permits_re_creating_an_existing_branch(self, project):
        """A plain collision is create_scope's own path to report, not this
        guard's — it must not mask the specific error with a generic one."""
        _assert_branch_available(project / ".bare", "fabfos/dev")
