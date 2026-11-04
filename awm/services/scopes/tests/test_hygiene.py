"""The scope hygiene check (scopes.md R45), on real git."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from awm.scopes import claims, hygiene
from awm.scopes import scopes as mod
from awm.scopes.models import ScopeCreateRequest

pytestmark = [pytest.mark.scopes, pytest.mark.slow, pytest.mark.subprocess]

PROJECT = "proj"
LATER = time.time() + 30 * 86400


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(worktree: Path, name: str) -> None:
    (worktree / name).write_text(name)
    _git(worktree, "add", name)
    _git(worktree, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name)


@pytest.fixture
def project(scopes_workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(claims, "waiting_sessions", lambda: set())
    posts: list[dict] = []
    monkeypatch.setattr(hygiene, "post", posts.append)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "README")
    proj = scopes_workspace["projects_dir"] / PROJECT
    proj.mkdir()
    bare = proj / ".bare"
    subprocess.run(["git", "clone", "--bare", "-q", str(seed), str(bare)], check=True, capture_output=True)
    return {"proj": proj, "bare": bare, "posts": posts}


def _scope(name: str) -> Path:
    mod.create_scope(ScopeCreateRequest(project=PROJECT, scope=name))
    return mod.PROJECTS_DIR / PROJECT / name


def test_a_merged_idle_clean_scope_is_removed_and_its_branch_kept(project):
    wt = _scope("old")
    report = hygiene.check(now=LATER)
    assert [r["scope"] for r in report["removed"]] == ["old"]
    assert not wt.exists()
    assert _git(project["bare"], "rev-parse", "refs/heads/feat/old")
    assert project["posts"] and project["posts"][0]["removed"][0]["branch"] == "feat/old"


def test_an_unmerged_scope_is_held_with_the_reason(project):
    wt = _scope("work")
    _commit(wt, "change")
    report = hygiene.check(now=LATER)
    assert report["removed"] == [] and "not merged" in report["held"][0]["why"]
    assert wt.exists()


def test_a_recently_used_scope_is_held(project):
    wt = _scope("fresh")
    report = hygiene.check()
    assert report["removed"] == [] and "day(s) ago" in report["held"][0]["why"] and wt.exists()


def test_a_claimed_scope_is_held(project):
    _scope("busy")
    claims.claim(PROJECT, "busy", "cc" + "b" * 32, pid=os.getpid())
    report = hygiene.check(now=LATER)
    assert "in use" in report["held"][0]["why"]


def test_a_dirty_scope_is_held(project):
    wt = _scope("dirty")
    (wt / "notes.txt").write_text("x")
    report = hygiene.check(now=LATER)
    assert "uncommitted" in report["held"][0]["why"] and wt.exists()


def test_a_dry_run_removes_nothing_and_posts_nothing(project):
    wt = _scope("old")
    report = hygiene.check(now=LATER, dry_run=True)
    assert report["removed"][0]["dry_run"] and wt.exists() and not project["posts"]


def test_every_tenth_create_runs_a_pass(project, monkeypatch):
    runs = []
    monkeypatch.setattr(hygiene, "_run", lambda: runs.append(1))
    counts = [hygiene.after_create(background=False) for _ in range(20)]
    assert counts[-1] == 20 and len(runs) == 2
    assert hygiene.status() == {"creates": 20, "next_check_after": 10}
