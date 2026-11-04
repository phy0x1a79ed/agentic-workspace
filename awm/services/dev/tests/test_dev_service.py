"""Tests for the `dev` service: manifest/handler shape and which worktree a
lifecycle call may drive. ``run_lifecycle`` shells through a mocked
``subprocess.run``; no gateway runs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awm.dev import dev
from awm.dev import hub_adapter


def test_manifest_tools_are_dev_prefixed():
    tools = [f["tool"] for f in hub_adapter.API_MANIFEST["functions"]]
    assert tools == ["dev_start", "dev_stop", "dev_restart", "dev_seed", "dev_status"]


def test_every_function_takes_the_worktree():
    for fn in hub_adapter.API_MANIFEST["functions"]:
        assert [p["name"] for p in fn["params"] if p.get("required")] == ["worktree"]


def test_handlers_cover_every_manifest_function():
    fns = {f["name"] for f in hub_adapter.API_MANIFEST["functions"]}
    assert set(hub_adapter.HANDLERS) == fns


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_WORKSPACE", str(tmp_path))
    tree = tmp_path / "projects" / "awm" / "web-ui"
    script = tree / dev.SANDBOX
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n")
    return tree


def test_lifecycle_runs_the_named_worktrees_sandbox(workspace, monkeypatch):
    calls = {}

    def _fake_run(argv, **kw):
        calls["argv"], calls["cwd"] = argv, kw.get("cwd")
        return SimpleNamespace(returncode=0, stdout="[dev] ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = hub_adapter.HANDLERS["restart"]({"worktree": str(workspace)})
    assert out == {"action": "restart", "rc": 0, "stdout": "[dev] ok\n", "stderr": ""}
    assert calls["argv"] == ["bash", str(workspace / dev.SANDBOX), "restart"]
    assert calls["cwd"] == str((workspace / dev.SANDBOX).parent)


@pytest.mark.parametrize("worktree, why", [
    ("", "absolute"),
    ("projects/awm/web-ui", "absolute"),
    ("/etc", "not under"),
])
def test_lifecycle_refuses_a_worktree_outside_the_workspace(workspace, monkeypatch, worktree, why):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran a script"))
    out = dev.run_lifecycle("start", worktree)
    assert out["rc"] == 2 and why in out["error"]


def test_lifecycle_refuses_a_worktree_without_a_sandbox(workspace, monkeypatch):
    bare = workspace.parent / "bare"
    bare.mkdir()
    out = dev.run_lifecycle("start", str(bare))
    assert out["rc"] == 2 and "no dev sandbox" in out["error"]


def test_lifecycle_rejects_unknown_action(workspace):
    out = dev.run_lifecycle("nuke", str(workspace))
    assert out["rc"] == 2 and "unknown" in out["error"]


def test_the_sandbox_script_ships_in_this_tree():
    here = Path(__file__).resolve().parents[1] / "sandbox"
    assert (here / "run.sh").is_file() and (here / "seed.py").is_file()
    assert "gateway serve --state" in (here / "run.sh").read_text()
