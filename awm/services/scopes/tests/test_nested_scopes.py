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

from awm.scopes.scopes import (
    _assert_branch_available,
    _cleanup_worktree,
    _live_worktrees_under,
    _project_worktrees,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def project(scopes_workspace, tmp_path: Path):
    """A project bare repo with a flat scope and two nested ones.

    Built on ``scopes_workspace`` so the service DB is redirected too — a test
    that reaches ``create_scope`` must not read the real workspace's rows.
    """
    projects_dir = scopes_workspace["projects_dir"]
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

    def test_raises_rather_than_returning_a_short_list(self, scopes_workspace):
        """An empty list would read as 'keep nothing' to data_gc."""
        with pytest.raises(FileNotFoundError):
            _project_worktrees("no-such-project")


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


class TestContainerDirectoryIsNotAScope:
    """``projects/<p>/fabfos`` holds the scope ``fabfos/dev``; it is not itself a
    scope. Nothing in the lifecycle may treat it as a removable worktree.

    This is not hypothetical: asking for a scope literally named ``fabfos``
    rmtree'd the directory — and the live scope inside it — before the branch
    check that would have refused the request ever ran.
    """

    def test_live_worktrees_under_sees_the_nested_scope(self, project):
        found = _live_worktrees_under(project / ".bare", project / "fabfos")
        assert [p.resolve() for p in found] == [(project / "fabfos" / "dev").resolve()]

    def test_a_scope_does_not_contain_itself(self, project):
        assert _live_worktrees_under(project / ".bare", project / "monorepo") == []

    def test_cleanup_refuses_a_container_directory(self, project):
        with pytest.raises(RuntimeError, match="not a scope"):
            _cleanup_worktree(project / ".bare", project / "fabfos", "fabfos")
        assert (project / "fabfos" / "dev").exists()
        assert (project / "fabfos" / "dev" / ".git").exists()

    def test_create_validates_the_branch_before_it_removes_anything(self, project):
        """The ordering bug itself: a request that cannot succeed must not have
        already deleted a tree by the time it is refused."""
        from awm.scopes.models import ScopeCreateRequest
        from awm.scopes import scopes as mod

        with pytest.raises((ValueError, RuntimeError)):
            mod.create_scope(ScopeCreateRequest(
                project="metasmith", scope="fabfos",
                branch_name="fabfos", from_branch="main",
            ))
        assert (project / "fabfos" / "dev" / ".git").exists(), \
            "the refused request destroyed a live nested scope"

    def test_cleanup_still_removes_a_stale_directory(self, project):
        """The container guard must not block the case it shares a shape with:
        a leftover directory with no live worktree beneath it."""
        stale = project / "stale"
        stale.mkdir()
        (stale / "leftover.txt").write_text("x")
        _cleanup_worktree(project / ".bare", stale, "feat/stale")
        assert not stale.exists()
