"""Shared fixtures: an isolated environment and a queue in a temp dir."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from awm.reporting import reconcile  # noqa: E402
from awm.reporting.store import Queue  # noqa: E402


@pytest.fixture(autouse=True)
def door_env(monkeypatch, tmp_path):
    """A fleet node of swarm `tony` with the door switched on and every path in tmp."""
    monkeypatch.setenv("AWM_FRONT_DOOR", "1")
    monkeypatch.setenv("AWM_NODE_ROLE", "fleet")
    monkeypatch.setenv("AWM_SWARM", "tony")
    monkeypatch.setenv("AWM_REPORTING_DB", str(tmp_path / "reporting.db"))
    monkeypatch.setenv("AWM_REPORTING_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    for name in ("AWM_BOARD_URL", "AWM_BOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)


class FakePins:
    def __init__(self) -> None:
        self.pinned: list[str] = []
        self.released: list[str] = []
        self.forgotten: set[str] = set()

    async def pin(self, job: str) -> None:
        self.pinned.append(job)

    def known(self, job: str) -> bool:
        return job not in self.forgotten

    async def release(self, job: str) -> None:
        self.released.append(job)


@pytest.fixture(autouse=True)
def no_live_claude(monkeypatch, tmp_path):
    """Nothing here may pin, remove or read a real Claude Code job."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(reconcile, "Pins", FakePins)


@pytest.fixture
def queue(tmp_path):
    return Queue(tmp_path / "reporting.db")
