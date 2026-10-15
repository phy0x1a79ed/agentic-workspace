"""Real-git tests for create on a branch that already exists, and for branch survival.

The bug these guard (journal 472b7d07): create always ran ``worktree add -b``,
so an existing branch was refused, and the rollback of that refusal ran
``branch -D`` on the branch it had just failed to create — a branch holding
commits no remote had. Every survival test below therefore commits to the
branch in a scratch worktree first, so the commit exists on that ref alone.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

import subprocess
from pathlib import Path

from awm.gatewayclient import cancel
from awm.scopes import scopes as mod
from awm.scopes.identity import agent_id_for_scope
from awm.scopes.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.scopes.operations.scopes import SCOPE_MANIFEST_FUNCTIONS

PROJECT = "proj"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(worktree: Path, name: str) -> str:
    (worktree / name).write_text(name)
    _git(worktree, "add", name)
    _git(worktree, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name)
    return _git(worktree, "rev-parse", "HEAD")


@pytest.fixture
def repos(scopes_workspace, tmp_path: Path):
    """A project bare with two remotes, origin and capella, both fetched."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "README")

    remotes = {}
    for name in ("origin", "capella"):
        remotes[name] = tmp_path / f"{name}.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(remotes[name])],
                       check=True, capture_output=True)

    proj = scopes_workspace["projects_dir"] / PROJECT
    proj.mkdir()
    bare = proj / ".bare"
    subprocess.run(["git", "clone", "--bare", "-q", str(remotes["origin"]), str(bare)],
                   check=True, capture_output=True)
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "remote", "add", "capella", str(remotes["capella"]))
    _git(bare, "fetch", "-q", "--all")
    return {"proj": proj, "bare": bare, "seed": seed, "tmp": tmp_path, **remotes}


def _publish(repos, branch: str, *remotes: str) -> str:
    """Put ``branch`` with one commit on each named remote, then fetch."""
    seed = repos["seed"]
    _git(seed, "checkout", "-q", "-B", branch, "main")
    sha = _commit(seed, branch.replace("/", "_"))
    _git(seed, "checkout", "-q", "main")
    for remote in remotes:
        _git(seed, "push", "-q", str(repos[remote]), f"{branch}:{branch}")
    _git(repos["bare"], "fetch", "-q", "--all")
    return sha


def _unpushed_branch(repos, branch: str) -> str:
    """A local branch carrying a commit that exists nowhere else."""
    scratch = repos["tmp"] / "scratch"
    _git(repos["bare"], "worktree", "add", "-q", "-b", branch, str(scratch), "main")
    sha = _commit(scratch, "unpushed")
    _git(repos["bare"], "worktree", "remove", str(scratch))
    return sha


def _sha(repos, ref: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repos["bare"]), "rev-parse", "--verify", "--quiet", ref],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def _upstream(worktree: Path, branch: str) -> str:
    return _git(worktree, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")


def _create(scope: str, **kw):
    return mod.create_scope(ScopeCreateRequest(project=PROJECT, scope=scope, **kw))


def _assert_rolled_back(repos, scope: str):
    assert not (repos["proj"] / scope).exists()
    assert agent_id_for_scope(PROJECT, scope) is None


class TestCreateAdoptsAnExistingBranch:
    def test_local_branch_with_an_unpushed_commit(self, repos):
        sha = _unpushed_branch(repos, "feat/x")
        result = _create("x")
        wt = repos["proj"] / "x"
        assert _git(wt, "rev-parse", "HEAD") == sha
        assert _sha(repos, "refs/heads/feat/x") == sha
        assert "existing branch feat/x" in result.message

    def test_the_start_other_shape(self, repos):
        """cx start_other passes no branch_name: feat/<scope> is implied."""
        sha = _unpushed_branch(repos, "feat/other")
        _create("other")
        assert _git(repos["proj"] / "other", "rev-parse", "HEAD") == sha

    def test_branch_on_one_remote_only(self, repos):
        sha = _publish(repos, "feat/y", "origin")
        _create("y")
        wt = repos["proj"] / "y"
        assert _git(wt, "rev-parse", "HEAD") == sha
        assert _upstream(wt, "feat/y") == "origin/feat/y"

    def test_local_branch_gains_the_one_remote_as_upstream(self, repos):
        _publish(repos, "feat/z", "origin")
        _git(repos["bare"], "branch", "--no-track", "feat/z", "origin/feat/z")
        _create("z")
        assert _upstream(repos["proj"] / "z", "feat/z") == "origin/feat/z"

    def test_several_remotes_are_refused_by_name(self, repos):
        _publish(repos, "feat/amb", "origin", "capella")
        with pytest.raises(ValueError) as exc:
            _create("amb")
        assert "origin/feat/amb" in str(exc.value)
        assert "capella/feat/amb" in str(exc.value)
        _assert_rolled_back(repos, "amb")
        assert _sha(repos, "refs/heads/feat/amb") is None

    def test_from_branch_picks_the_remote(self, repos):
        _publish(repos, "feat/amb", "origin", "capella")
        _create("amb", from_branch="capella/feat/amb")
        assert _upstream(repos["proj"] / "amb", "feat/amb") == "capella/feat/amb"

    def test_local_branch_with_no_upstream_and_several_remotes_is_refused(self, repos):
        _publish(repos, "feat/svc", "origin", "capella")
        _git(repos["bare"], "branch", "--no-track", "feat/svc", "origin/feat/svc")
        with pytest.raises(ValueError, match="several remotes"):
            _create("svc")
        _create("svc", from_branch="capella/feat/svc")
        assert _upstream(repos["proj"] / "svc", "feat/svc") == "capella/feat/svc"

    def test_from_branch_naming_another_base_is_refused(self, repos):
        sha = _unpushed_branch(repos, "feat/x")
        with pytest.raises(ValueError, match="already exists"):
            _create("x", from_branch="main")
        assert _sha(repos, "refs/heads/feat/x") == sha

    def test_branch_checked_out_elsewhere_is_refused(self, repos):
        elsewhere = repos["proj"] / "elsewhere"
        _git(repos["bare"], "worktree", "add", "-q", "-b", "feat/held", str(elsewhere), "main")
        with pytest.raises(FileExistsError, match="elsewhere"):
            _create("held")
        assert (elsewhere / ".git").exists()
        _assert_rolled_back(repos, "held")


class TestAFailedCreateKeepsAPreExistingBranch:
    def test_failure_after_worktree_add(self, repos, monkeypatch):
        sha = _unpushed_branch(repos, "feat/x")

        def boom(*a, **k):
            raise RuntimeError("index down")
        monkeypatch.setattr(mod.search_index, "index_scope", boom)
        with pytest.raises(RuntimeError, match="index down"):
            _create("x")
        _assert_rolled_back(repos, "x")
        assert _sha(repos, "refs/heads/feat/x") == sha

    def test_cancel_during_provisioning(self, repos, monkeypatch):
        sha = _unpushed_branch(repos, "feat/x")

        def cancelled(*a, **k):
            raise cancel.Cancelled("call cancelled by the caller")
        monkeypatch.setattr(mod.data_dvc, "provision_scope_data", cancelled)
        with pytest.raises(cancel.Cancelled):
            _create("x")
        _assert_rolled_back(repos, "x")
        assert _sha(repos, "refs/heads/feat/x") == sha

    def test_failed_worktree_add(self, repos, monkeypatch):
        sha = _unpushed_branch(repos, "feat/x")
        real = mod.run_git

        def failing_add(cmd, **kw):
            if "worktree" in cmd and "add" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: simulated")
            return real(cmd, **kw)
        monkeypatch.setattr(mod, "run_git", failing_add)
        with pytest.raises(RuntimeError, match="simulated"):
            _create("x")
        _assert_rolled_back(repos, "x")
        assert _sha(repos, "refs/heads/feat/x") == sha

    def test_stale_directory_cleanup_keeps_the_branch(self, repos):
        sha = _unpushed_branch(repos, "feat/x")
        stale = repos["proj"] / "x"
        stale.mkdir()
        (stale / "leftover.txt").write_text("x")
        _create("x")
        assert not (stale / "leftover.txt").exists()
        assert _git(stale, "rev-parse", "HEAD") == sha

    def test_a_branch_this_call_made_is_still_removed(self, repos, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("index down")
        monkeypatch.setattr(mod.search_index, "index_scope", boom)
        with pytest.raises(RuntimeError):
            _create("fresh")
        _assert_rolled_back(repos, "fresh")
        assert _sha(repos, "refs/heads/feat/fresh") is None

    def test_a_made_branch_that_moved_survives_the_rollback(self, repos, monkeypatch):
        """The SHA guard: a commit landing on the new branch before rollback keeps it."""
        moved = {}

        def commit_then_fail(*a, **k):
            moved["sha"] = _commit(repos["proj"] / "fresh", "late")
            raise RuntimeError("index down")
        monkeypatch.setattr(mod.search_index, "index_scope", commit_then_fail)
        with pytest.raises(RuntimeError):
            _create("fresh")
        assert _sha(repos, "refs/heads/feat/fresh") == moved["sha"]


class TestDeleteAndCompleteKeepTheBranch:
    def _scope(self, repos, scope: str) -> str:
        _create(scope)
        return _commit(repos["proj"] / scope, "work")

    def test_delete_keeps_it_by_default(self, repos):
        sha = self._scope(repos, "d")
        result = mod.delete_scope(PROJECT, "d")
        assert not (repos["proj"] / "d").exists()
        assert _sha(repos, "refs/heads/feat/d") == sha
        assert "branch feat/d kept" in result.message

    def test_delete_branch_deletes_it_and_names_the_sha(self, repos):
        sha = self._scope(repos, "d")
        result = mod.delete_scope(PROJECT, "d", delete_branch=True)
        assert _sha(repos, "refs/heads/feat/d") is None
        assert f"git branch feat/d {sha}" in result.message

    def test_complete_cleanup_keeps_it_by_default(self, repos):
        sha = self._scope(repos, "c")
        mod.update_scope(PROJECT, "c", ScopeUpdateRequest(cleanup=True))
        assert not (repos["proj"] / "c").exists()
        assert _sha(repos, "refs/heads/feat/c") == sha

    def test_complete_delete_branch_deletes_it(self, repos):
        sha = self._scope(repos, "c")
        result = mod.update_scope(PROJECT, "c",
                                  ScopeUpdateRequest(cleanup=True, delete_branch=True))
        assert _sha(repos, "refs/heads/feat/c") is None
        assert sha in result.message

    def test_complete_delete_branch_without_cleanup_is_refused(self, repos):
        sha = self._scope(repos, "c")
        with pytest.raises(ValueError, match="cleanup"):
            mod.update_scope(PROJECT, "c", ScopeUpdateRequest(delete_branch=True))
        assert _sha(repos, "refs/heads/feat/c") == sha
        assert agent_id_for_scope(PROJECT, "c", active_only=True) is not None

    def test_delete_branch_refuses_a_branch_checked_out_elsewhere(self, repos):
        self._scope(repos, "d")
        mod.delete_scope(PROJECT, "d")
        other = repos["tmp"] / "other"
        _git(repos["bare"], "worktree", "add", "-q", str(other), "feat/d")
        assert mod._settle_branch(repos["bare"], "feat/d", True).startswith(
            "branch feat/d kept: branch 'feat/d' is checked out at")
        assert _sha(repos, "refs/heads/feat/d") is not None


class TestDescribeShowsTheOptIn:
    @pytest.mark.parametrize("verb", ["scope_delete", "scope_complete"])
    def test_delete_branch_param(self, verb):
        entry = next(f for f in SCOPE_MANIFEST_FUNCTIONS if f["name"] == verb)
        param = next(p for p in entry["params"] if p["name"] == "delete_branch")
        assert param["type"] == "boolean"
        assert param["required"] is False
        assert "Default false" in param["description"]
        assert "delete_branch" in entry["description"]


def test_branch_deletion_lives_in_one_helper():
    source = Path(mod.__file__).read_text()
    assert '"branch", "-D"' not in source
    assert source.count('"update-ref", "-d"') == 1
