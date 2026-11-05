"""The build stamp lives in the fork worktree's git dir, never in the worktree (scopes.md R142)."""

from __future__ import annotations

import subprocess

from awm.trilium import instances


def test_source_state_reads_the_build_stamp_from_the_git_dir(tmp_path, monkeypatch):
    fork = tmp_path / "fork"
    fork.mkdir()
    git = ["git", "-C", str(fork), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(instances, "FORK_DIR", fork)
    assert instances.source_state()["built_current"] is None
    (fork / ".git" / "awm").mkdir()
    (fork / ".git" / "awm" / "trilium-build-stamp").write_text(f"head={head} dirty=0 lock=x\n")
    assert instances.source_state()["built_current"] is True
    assert not (fork / ".awm").exists()
