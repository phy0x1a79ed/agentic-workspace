"""awm's own deploy.toml, the full-tests routine and its known-failures script."""

from __future__ import annotations

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
    assert cfg.nodes[0] == cfg.canonical == "mizar"


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
    (repo / "awm/gateway/scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts/full-tests.sh", repo / "scripts/full-tests.sh")
    fake = repo / "awm/gateway/scripts/run-tests.sh"
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
