"""The trusted-directory checks, against a fake `~/.claude.json`."""
from __future__ import annotations

import json
import subprocess

import pytest

from awm.claudedaemon import trust


def trust_file(tmp_path, projects):
    f = tmp_path / "claude.json"
    f.write_text(json.dumps({"projects": projects}))
    return f


def test_trust_is_inherited_from_an_ancestor(tmp_path):
    f = trust_file(tmp_path, {str(tmp_path): {"hasTrustDialogAccepted": True}})
    assert trust.trusted(tmp_path, trust_file=f)
    assert trust.trusted(tmp_path / "a" / "b", trust_file=f)


def test_a_directory_with_no_trusted_ancestor_is_untrusted(tmp_path):
    f = trust_file(tmp_path, {str(tmp_path / "other"): {"hasTrustDialogAccepted": True}})
    assert not trust.trusted(tmp_path / "here", trust_file=f)


def test_a_declined_entry_is_not_trust(tmp_path):
    f = trust_file(tmp_path, {str(tmp_path): {"hasTrustDialogAccepted": False}})
    assert not trust.trusted(tmp_path, trust_file=f)


def test_an_unreadable_or_reshaped_file_reads_as_untrusted(tmp_path):
    assert not trust.trusted(tmp_path, trust_file=tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert not trust.trusted(tmp_path, trust_file=bad)
    bad.write_text(json.dumps({"projects": ["not", "a", "dict"]}))
    assert not trust.trusted(tmp_path, trust_file=bad)
    bad.write_text("[]")
    assert not trust.trusted(tmp_path, trust_file=bad)


# --- the stricter rule for `claude --bg` -------------------------------------


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "protocol.file.allow=always", *args],
        cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path):
    """A plain repo, and a bare clone of it with one linked worktree."""
    plain = tmp_path / "plain"
    plain.mkdir()
    git("init", "-q", cwd=plain)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=plain)
    bare = tmp_path / "proj" / ".bare"
    bare.parent.mkdir()
    git("clone", "-q", "--bare", str(plain), str(bare), cwd=tmp_path)
    wt = tmp_path / "proj" / "scope"
    git("worktree", "add", "-q", "-b", "feat/scope", str(wt), cwd=bare)
    return plain.resolve(), bare.resolve(), wt.resolve()


def test_an_ancestor_does_not_trust_a_git_worktree(tmp_path, repos):
    plain, bare, wt = repos
    f = trust_file(tmp_path, {str(tmp_path): {"hasTrustDialogAccepted": True}})
    assert trust.trusted(wt, trust_file=f)  # the `/cd` rule still inherits
    why = trust.start_refusal(wt, trust_file=f)
    assert why and str(bare) in why and "claude" in why and "accept" in why


def test_the_main_repository_entry_trusts_every_worktree(tmp_path, repos):
    plain, bare, wt = repos
    f = trust_file(tmp_path, {str(bare): {"hasTrustDialogAccepted": True}})
    assert trust.start_refusal(wt, trust_file=f) is None


def test_a_declined_repository_entry_is_not_trust(tmp_path, repos):
    plain, bare, wt = repos
    f = trust_file(tmp_path, {str(bare): {"hasTrustDialogAccepted": False}})
    assert trust.start_refusal(wt, trust_file=f)


def test_a_plain_repository_needs_its_own_entry(tmp_path, repos):
    plain, bare, wt = repos
    (plain / "sub").mkdir()
    ancestor = trust_file(tmp_path, {str(tmp_path): {"hasTrustDialogAccepted": True}})
    why = trust.start_refusal(plain / "sub", trust_file=ancestor)
    assert why and str(plain) in why
    own = trust_file(tmp_path, {str(plain): {"hasTrustDialogAccepted": True}})
    assert trust.start_refusal(plain, trust_file=own) is None
    assert trust.start_refusal(plain / "sub", trust_file=own) is None


def test_the_workspace_key_is_the_bare_dir_or_the_repo_root(repos):
    plain, bare, wt = repos
    assert trust.workspace_key(wt) == bare
    assert trust.workspace_key(plain) == plain


def test_an_inherited_git_environment_does_not_redirect_the_lookup(repos, monkeypatch):
    plain, bare, wt = repos
    monkeypatch.setenv("GIT_DIR", str(plain / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(plain))
    monkeypatch.setenv("GIT_COMMON_DIR", str(plain / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(plain / ".git" / "index"))
    assert trust.workspace_key(wt) == bare


def test_a_directory_outside_git_inherits_from_an_ancestor(tmp_path):
    outside = tmp_path / "notgit"
    outside.mkdir()
    f = trust_file(tmp_path, {str(tmp_path): {"hasTrustDialogAccepted": True}})
    assert trust.workspace_key(outside) is None
    assert trust.start_refusal(outside, trust_file=f) is None
    why = trust.start_refusal(outside, trust_file=trust_file(tmp_path, {}))
    assert why and str(outside) in why and "claude" in why


def test_an_unreadable_trust_file_refuses_a_start(tmp_path, repos):
    assert trust.start_refusal(repos[2], trust_file=tmp_path / "missing.json")
