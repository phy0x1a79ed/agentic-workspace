"""awm's own deploy.toml, the full-tests routine and its known-failures script."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awm.routine import definitions, deployconf

from tests.conftest import git

pytestmark = pytest.mark.smoke

REPO = Path(__file__).resolve().parents[4]


def test_awm_deploy_toml_parses():
    cfg = deployconf.parse((REPO / "deploy.toml").read_text())
    assert cfg.check_timeout == 60 and "--smoke" in cfg.check
    assert cfg.refresh.kind == "queue"
    assert cfg.refresh.sync == "scripts/refresh-checkout.sh"
    assert (REPO / cfg.refresh.sync).is_file()
    assert "FEDERATION.md § Deploy" in cfg.refresh.prompt
    assert cfg.backup == "github"
    assert cfg.fixer_peer == "local"


def _node(tmp_path):
    """A canonical bare repo with refs/mizar/release, and a node checkout of it."""
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "release", str(seed))
    (seed / "keep.txt").write_text("1\n")
    (seed / "gone.txt").write_text("1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "one")
    bare = tmp_path / "bare"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    node = tmp_path / "node"
    git(tmp_path, "clone", "-q", "-o", "awm", "-b", "release", str(bare), str(node))
    (seed / "gone.txt").unlink()
    (seed / "keep.txt").write_text("2\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "two")
    to = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "-q", str(bare), "HEAD:refs/mizar/release")
    (node / ".awm").mkdir()
    (node / ".awm" / "state.db").write_text("x")
    (node / "stray.txt").write_text("x")
    return node, to


def _refresh(node, to):
    return subprocess.run(["sh", str(REPO / "scripts/refresh-checkout.sh")], cwd=node,
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                               "AWM_REFRESH_TO": to})


def test_refresh_checkout_takes_a_pushed_release(tmp_path):
    node, to = _node(tmp_path)
    (node / ".gitignore").write_text(".awm/\n")
    r = _refresh(node, to)
    assert r.returncode == 0, r.stderr
    assert git(node, "rev-parse", "HEAD") == to
    assert (node / "keep.txt").read_text() == "2\n" and not (node / "gone.txt").exists()
    assert not (node / "stray.txt").exists() and (node / ".awm" / "state.db").exists()


def test_refresh_checkout_keeps_a_local_commit(tmp_path):
    node, to = _node(tmp_path)
    git(node, "commit", "-q", "--allow-empty", "-m", "hand patch")
    head = git(node, "rev-parse", "HEAD")
    r = _refresh(node, to)
    assert r.returncode == 1 and "does not contain" in r.stderr
    assert git(node, "rev-parse", "HEAD") == head


def test_awm_deploy_toml_names_no_host():
    # fleet.md R140: the node list lives in each node's install config.
    assert "[fleet]" not in (REPO / "deploy.toml").read_text()


def test_full_tests_routine_parses():
    r = definitions.load(REPO / "routines" / "full-tests.toml")
    assert r.timer and [s.run for s in r.steps] == ["scripts/full-tests.sh"]


FAKE_RUN_TESTS = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in --summary) out="$2"; shift 2 ;; *) shift ;; esac
done
cat "$(git rev-parse --show-toplevel)/results" > "$out"
"""


def test_full_tests_flags_only_new_failures(tmp_path):
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts/full-tests.sh", repo / "scripts/full-tests.sh")
    fake = repo / "scripts/run-tests.sh"
    fake.write_text(FAKE_RUN_TESTS)
    fake.chmod(0o755)
    git(repo, "init", "-q")
    (repo / "results").write_text("a PASS\nb FAIL (rc=1)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")

    def run():
        return subprocess.run(["bash", "scripts/full-tests.sh"], cwd=repo,
                              capture_output=True, text=True)

    first = run()
    assert first.returncode == 1 and "new failures: b" in first.stdout
    head = git(repo, "rev-parse", "HEAD")
    known = repo / ".awm/full-tests/known-failures"
    assert known.read_text().split() == ["b", head]
    # The same failure the next night is known, not new.
    second = run()
    assert second.returncode == 0 and "no new failures" in second.stdout
    # b recovers, a breaks: only a is new, and b leaves the known list.
    (repo / "results").write_text("a FAIL (rc=1)\nb PASS\n")
    third = run()
    assert third.returncode == 1 and "new failures: a" in third.stdout
    assert known.read_text().split()[0] == "a"


def _move(ws, *args):
    return subprocess.run(["bash", str(REPO / "scripts/move-canonical.sh"), *args],
                          capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
                               "AWM_WORKSPACE": str(ws)})


def test_move_canonical_repoints_the_checkout_and_adds_github(tmp_path):
    old = tmp_path / "old.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "release", str(seed))
    git(seed, "commit", "-q", "--allow-empty", "-m", "one")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(old))
    ws = tmp_path / "ws"
    git(tmp_path, "clone", "-q", "-o", "awm", str(old), str(ws))
    git(ws, "remote", "add", "origin", str(old))
    git(ws, "remote", "add", "other", str(seed))
    bare = ws / "projects/awm/.bare"
    git(tmp_path, "clone", "-q", "--bare", str(old), str(bare))
    r = _move(ws, "repoint", str(old), "local")
    assert r.returncode == 0, r.stderr
    assert git(ws, "remote", "get-url", "awm") == str(bare)
    assert git(ws, "remote", "get-url", "origin") == str(bare)
    assert git(ws, "remote", "get-url", "other") == str(seed)
    gh = tmp_path / "gh.git"
    git(tmp_path, "init", "-q", "--bare", str(gh))
    r = _move(ws, "github", str(gh))
    assert r.returncode == 0 and "first deploy" in r.stdout
    git(bare, "push", "-q", "github", "release")
    git(seed, "commit", "-q", "--allow-empty", "-m", "made on github only")
    git(seed, "push", "-q", str(gh), "release")
    r = _move(ws, "github", str(gh))
    assert r.returncode == 1 and "stops at backup" in r.stderr
    assert "canonical_repos" in _move(ws, "order").stdout
