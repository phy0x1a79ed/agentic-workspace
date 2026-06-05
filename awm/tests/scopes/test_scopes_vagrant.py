"""Tests for vagrant-scopes (sentinel ``_vagrant`` project + unified repo)."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

import pytest

from awm.config import VAGRANT_PROJECT
from awm.models import ProjectCreateRequest, ScopeCreateRequest
from awm.services import projects as projects_svc
from awm.services import scopes as scopes_svc
from awm.services.config_service import (
    get_config,
    get_vagrant_scopes_repo_url,
    set_config,
)


# ---------------------------------------------------------------------------
# Reserved sentinel project name
# ---------------------------------------------------------------------------

def test_reserved_project_name_blocks_create_project(awm_workspace):
    req = ProjectCreateRequest(name=VAGRANT_PROJECT)
    with pytest.raises(ValueError, match="reserved for vagrant scopes"):
        projects_svc.create_project(req)


# ---------------------------------------------------------------------------
# Config-driven repo URL
# ---------------------------------------------------------------------------

def test_vagrant_repo_url_defaults_to_github_user(awm_workspace):
    url = get_vagrant_scopes_repo_url()
    assert url.endswith("/vagrant-scopes.git")
    assert "github.com" in url


def test_vagrant_repo_url_config_roundtrip(awm_workspace):
    custom = "git@github.com:other-org/elsewhere.git"
    set_config("vagrant_scopes_repo_url", custom)
    assert get_vagrant_scopes_repo_url() == custom
    assert get_config("vagrant_scopes_repo_url") == custom


# ---------------------------------------------------------------------------
# create_scope precondition with sentinel
# ---------------------------------------------------------------------------

def test_create_scope_vagrant_without_bootstrap_raises_with_hint(awm_workspace):
    req = ScopeCreateRequest(project=VAGRANT_PROJECT, scope="anything")
    with pytest.raises(FileNotFoundError, match="awm vagrant-init"):
        scopes_svc.create_scope(req)


# ---------------------------------------------------------------------------
# ensure_vagrant_repo() bootstrap
# ---------------------------------------------------------------------------

@pytest.fixture
def vagrant_bootstrap(awm_workspace, monkeypatch):
    """Bootstrap the unified vagrant repo without touching the GitHub side."""
    # shutil.which("gh") → None inside the service module skips the GitHub
    # bootstrap path; the local bare repo is all the test needs.
    import shutil as _shutil
    real_which = _shutil.which
    monkeypatch.setattr(
        "awm.services.scopes.shutil.which",
        lambda name: None if name == "gh" else real_which(name),
    )
    scopes_svc.ensure_vagrant_repo()
    return awm_workspace


def test_ensure_vagrant_repo_creates_bare_repo(vagrant_bootstrap):
    bare = vagrant_bootstrap["projects_dir"] / VAGRANT_PROJECT / ".bare"
    assert bare.exists()
    # Has main branch with an initial commit.
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "refs/heads/main"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def test_ensure_vagrant_repo_is_idempotent(vagrant_bootstrap):
    # Second call must not throw, and the bare must still be valid.
    scopes_svc.ensure_vagrant_repo()
    bare = vagrant_bootstrap["projects_dir"] / VAGRANT_PROJECT / ".bare"
    assert bare.exists()


def test_ensure_vagrant_repo_uses_config_url(vagrant_bootstrap):
    # Repoint the URL, re-run bootstrap, confirm origin updated.
    new_url = "git@github.com:alt/vagrant-scopes.git"
    set_config("vagrant_scopes_repo_url", new_url)
    scopes_svc.ensure_vagrant_repo()
    bare = vagrant_bootstrap["projects_dir"] / VAGRANT_PROJECT / ".bare"
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(bare), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == new_url


# ---------------------------------------------------------------------------
# create_scope under the sentinel project, then listing
# ---------------------------------------------------------------------------

def test_create_scope_in_vagrant_project_succeeds(vagrant_bootstrap):
    req = ScopeCreateRequest(project=VAGRANT_PROJECT, scope="smoke")
    resp = scopes_svc.create_scope(req)
    assert resp.status == "active"
    worktree = vagrant_bootstrap["projects_dir"] / VAGRANT_PROJECT / "smoke"
    assert worktree.exists()
    assert (worktree / ".awm" / "context.md").exists()

    # Row exists with project='_vagrant'.
    listed = scopes_svc.search_scopes(status="all", project=VAGRANT_PROJECT)
    assert listed.total == 1
    assert listed.scopes[0].scope == "smoke"
    assert listed.scopes[0].project == VAGRANT_PROJECT


def test_list_scopes_filters_vagrant_distinct_from_resident(
    vagrant_bootstrap, seeded_scopes
):
    # seeded_scopes has rows for proj-a / proj-b only; vagrant project listed
    # separately should not include them.
    req = ScopeCreateRequest(project=VAGRANT_PROJECT, scope="alpha")
    scopes_svc.create_scope(req)

    vagrant_only = scopes_svc.search_scopes(status="all", project=VAGRANT_PROJECT)
    assert {s.scope for s in vagrant_only.scopes} == {"alpha"}

    resident_only = scopes_svc.search_scopes(status="all", project="proj-a")
    assert all(s.project == "proj-a" for s in resident_only.scopes)


# ---------------------------------------------------------------------------
# Slug helper sanity
# ---------------------------------------------------------------------------

def test_vagrant_scope_name_slug():
    assert scopes_svc._vagrant_scope_name("user:operator") == "operator-handler"
    assert scopes_svc._vagrant_scope_name("user:tony@host") == "tony-host-handler"
    assert scopes_svc._vagrant_scope_name("") == "anon-handler"
