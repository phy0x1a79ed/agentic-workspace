"""The inventory of what neither remote holds."""

from __future__ import annotations

import os
import subprocess

import pytest

from awm.dvc import coverage


def git(repo, *args):
    # HOME is redirected at the repo so the developer's own gitconfig cannot
    # change what these assertions see.
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """projects/<project>/<scope>, the layout a scope worktree actually sits in."""
    scope = tmp_path / "projects" / "proj" / "dev"
    scope.mkdir(parents=True)
    git(scope, "init", "-q", "-b", "feat/dev")
    (scope / "README").write_text("hello\n")
    git(scope, "add", "README")
    git(scope, "commit", "-qm", "first")

    cache = tmp_path / "data" / ".dvc_cache"
    cache.mkdir(parents=True)
    monkeypatch.setattr(coverage, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(coverage, "SHARED_CACHE", cache)
    return tmp_path, scope, cache


def test_scopes_are_found_at_project_scope_depth_and_no_deeper(workspace):
    root, scope, _ = workspace
    # A vendored checkout inside a scope is not a scope; recursing would report
    # every third-party repo a project happens to have cloned.
    vendored = scope / "vendor" / "someone-elses-repo"
    vendored.mkdir(parents=True)
    git(vendored, "init", "-q")

    assert coverage.find_worktrees(root) == [scope]


def test_a_committed_pushed_scope_with_no_pins_is_not_at_risk(workspace):
    root, scope, _ = workspace
    # Give it an upstream that is exactly HEAD — the "everything is safe" case.
    bare = root / "bare.git"
    git(scope, "init", "-q", "--bare", str(bare))
    git(scope, "remote", "add", "origin", str(bare))
    git(scope, "push", "-q", "-u", "origin", "feat/dev")

    out = coverage.report()

    assert out["scopes"] == 1
    assert out["scopes_at_risk"] == 0
    assert out["worktrees"] == []


def test_untracked_and_modified_files_put_a_scope_at_risk(workspace):
    _, scope, _ = workspace
    (scope / "README").write_text("edited\n")
    (scope / "scratch.out").write_text("nowhere else\n")

    row = coverage.report()["worktrees"][0]

    assert row["uncommitted"]["modified"] == 1
    assert row["uncommitted"]["untracked"] == 1
    assert "scratch.out" in row["uncommitted"]["untracked_sample"]
    assert row["at_risk"] is True


def test_a_branch_with_no_upstream_is_as_lost_as_one_never_pushed(workspace):
    row = coverage.report()["worktrees"][0]

    assert row["unpushed"]["branch"] == "feat/dev"
    assert row["unpushed"]["no_upstream"] is True
    assert row["at_risk"] is True


def test_commits_ahead_of_the_upstream_are_counted(workspace):
    root, scope, _ = workspace
    bare = root / "bare.git"
    git(scope, "init", "-q", "--bare", str(bare))
    git(scope, "remote", "add", "origin", str(bare))
    git(scope, "push", "-q", "-u", "origin", "feat/dev")
    (scope / "README").write_text("more\n")
    git(scope, "commit", "-qam", "second")

    row = coverage.report()["worktrees"][0]

    assert row["unpushed"]["ahead"] == 1
    assert row["at_risk"] is True


def test_a_pin_the_shared_cache_cannot_satisfy_is_reported_missing(workspace):
    _, scope, _ = workspace
    (scope / ".dvc").mkdir()
    (scope / "data").mkdir()
    (scope / "data" / "chunk.dvc").write_text(
        "outs:\n- md5: " + "a" * 32 + "\n  path: chunk\n"
    )

    row = coverage.report()["worktrees"][0]

    assert row["pins"]["dvc"] is True
    assert row["pins"]["pins"] == 1
    assert row["pins"]["missing"] == 1


def test_a_missing_dir_manifest_is_reported_separately_from_missing_leaves(workspace):
    """Leaves under an absent manifest are not merely missing — they are unnameable.

    Folding them into `missing` would understate the gap for exactly the scopes
    in the worst shape, since their leaf count reads as zero.
    """
    _, scope, _ = workspace
    (scope / ".dvc").mkdir()
    (scope / "data").mkdir()
    (scope / "data" / "tree.dvc").write_text(
        "outs:\n- md5: " + "b" * 32 + ".dir\n  path: tree\n"
    )

    row = coverage.report()["worktrees"][0]

    assert row["pins"]["unresolved_manifests"] == 1
    assert row["at_risk"] is True


def test_all_includes_the_scopes_with_nothing_at_risk(workspace):
    out = coverage.report(at_risk_only=False)

    assert len(out["worktrees"]) == out["scopes"] == 1
    assert out["truncated"] is False
