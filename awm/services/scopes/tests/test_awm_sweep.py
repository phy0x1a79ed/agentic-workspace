"""The R142 sweep, run on a copy of the workspace layout: bare repos, nested scopes, old `.awm/`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awm.dvcdata import data_dvc
from awm.scopes import awm_sweep

pytestmark = [pytest.mark.scopes, pytest.mark.subprocess]

OLD_HOOK = """#!/usr/bin/env bash
# AWM-managed. Materialise the data this commit pins, after a merge moved it.
SENTINEL=".awm/data-checkout-failed"
MOUNTS=".awm/data-mounts"
"""


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def _project(projects: Path, name: str, scopes: list[str], *, dvc: bool = False) -> dict[str, Path]:
    seed = projects.parent / f"seed-{name}"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("x\n")
    if dvc:
        (seed / ".dvc").mkdir()
        (seed / ".dvc" / "config").write_text("[core]\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    bare = projects / name / ".bare"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    out = {}
    for scope in ["main", *scopes]:
        wt = projects / name / scope
        args = [str(wt), "main"] if scope == "main" else ["-b", f"feat/{scope}", str(wt), "main"]
        _git(bare, "worktree", "add", "-q", *args)
        out[scope] = wt
    return out


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    """Three projects shaped like the live ones, each worktree with an old `.awm/`."""
    ws = tmp_path / "workspace"
    projects = ws / "projects"
    projects.mkdir(parents=True)
    fake = tmp_path / "fake-dvc"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("AWM_DVC_BIN", str(fake))

    plain = _project(projects, "plain", ["dev", "lab/sub"])
    for scope, wt in plain.items():
        awm = wt / ".awm"
        awm.mkdir()
        (awm / "data").symlink_to(ws / "data" / "plain")
        (awm / "history.md").write_text("# generated\n")
        (awm / "mcp-opencode.json").write_text("{}\n")
        (awm / "skills").symlink_to(ws / "skills")
    (plain["dev"] / ".awm" / "context.md").write_text("# plain/dev\n\nFor the dev line.\n")

    tracked = _project(projects, "tracked", ["s1"], dvc=True)
    for wt in tracked.values():
        (wt / ".awm").mkdir()
        (wt / ".awm" / "data").symlink_to("../data")
    s1 = tracked["s1"] / ".awm"
    (s1 / data_dvc.MOUNTS_FILE).write_text("data/hot\n")
    (s1 / data_dvc.CHECKOUT_FAILED_FILE).write_text("dvc checkout FAILED after merge\n")
    hooks = projects / "tracked" / ".bare" / "hooks"
    (hooks / "post-merge").write_text(OLD_HOOK)

    odd = _project(projects, "odd", [])
    (odd["main"] / ".awm" / "data").mkdir(parents=True)
    (odd["main"] / ".awm" / "data" / "notes.txt").write_text("someone's files\n")

    yield {"projects": projects, "plain": plain, "tracked": tracked, "odd": odd}


def test_a_dry_run_reports_and_changes_nothing(layout):
    before = sorted(str(p) for p in layout["projects"].rglob("*") if ".awm" in p.parts)
    out = awm_sweep.sweep(layout["projects"], dry_run=True)
    assert out["errors"] == []
    assert sorted(str(p) for p in layout["projects"].rglob("*") if ".awm" in p.parts) == before
    by_wt = {Path(w["worktree"]).relative_to(layout["projects"]).as_posix(): w
             for w in out["worktrees"]}
    assert by_wt["plain/lab/sub"]["awm_dir"] == "removed"
    assert by_wt["plain/dev"]["left"] == ["context.md"]
    assert out["hooks"] == {"tracked": "would-rewrite"}


def test_the_sweep_empties_every_awm_dir_it_can(layout):
    out = awm_sweep.sweep(layout["projects"])
    assert out["errors"] == []
    for scope in ("main", "lab/sub"):
        assert not (layout["plain"][scope] / ".awm").exists()
    for wt in layout["tracked"].values():
        assert not (wt / ".awm").exists()
    assert out["left_behind"] == 2


def test_context_md_stays_for_the_briefs_one_shot(layout):
    awm_sweep.sweep(layout["projects"])
    left = sorted(p.name for p in (layout["plain"]["dev"] / ".awm").iterdir())
    assert left == ["context.md"]


def test_a_real_data_directory_is_left_and_reported(layout):
    out = awm_sweep.sweep(layout["projects"])
    odd = next(w for w in out["worktrees"] if w["project"] == "odd")
    assert odd["left"] == ["data"]
    assert (layout["odd"]["main"] / ".awm" / "data" / "notes.txt").exists()


def test_the_hook_state_moves_into_the_git_dir(layout):
    wt = layout["tracked"]["s1"]
    awm_sweep.sweep(layout["projects"])
    state = data_dvc.state_dir(wt)
    assert state == layout["projects"] / "tracked" / ".bare" / "worktrees" / "s1" / "awm"
    assert data_dvc.read_mounts(wt) == ["data/hot"]
    assert "FAILED after merge" in (state / data_dvc.CHECKOUT_FAILED_FILE).read_text()


def test_the_dvc_hooks_are_rewritten_to_the_git_dir(layout):
    out = awm_sweep.sweep(layout["projects"])
    assert out["hooks"]["tracked"]["post-merge_hook"] == "installed"
    hook = (layout["projects"] / "tracked" / ".bare" / "hooks" / "post-merge").read_text()
    assert '.awm/' not in hook and "git rev-parse --git-dir" in hook


def test_a_second_run_finds_nothing_to_do(layout):
    awm_sweep.sweep(layout["projects"])
    again = awm_sweep.sweep(layout["projects"])
    assert [w for w in again["worktrees"] if set(w) - {"project", "worktree", "left"}] == []
    assert again["hooks"]["tracked"] == {}


def test_a_build_stamp_moves_into_the_git_dir(layout):
    wt = layout["plain"]["main"]
    (wt / ".awm" / "dsh-build-stamp").write_text("head=abc dirty=0 lock=x\n")
    awm_sweep.sweep(layout["projects"])
    assert not (wt / ".awm").exists()
    assert (data_dvc.state_dir(wt) / "dsh-build-stamp").read_text() == "head=abc dirty=0 lock=x\n"
