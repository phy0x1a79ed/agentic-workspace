"""Shared fixtures: an isolated environment and a queue in a temp dir."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from awm.representative.store import Queue  # noqa: E402


@pytest.fixture(autouse=True)
def door_env(monkeypatch, tmp_path):
    """A fleet node of swarm `tony` with the door switched on and every path in tmp."""
    monkeypatch.setenv("AWM_FRONT_DOOR", "1")
    monkeypatch.setenv("AWM_NODE_ROLE", "fleet")
    monkeypatch.setenv("AWM_SWARM", "tony")
    monkeypatch.setenv("AWM_DOOR_DB", str(tmp_path / "door.db"))
    monkeypatch.setenv("AWM_DOOR_STATE", str(tmp_path / "state"))
    for name in ("AWM_BOARD_URL", "AWM_BOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def queue(tmp_path):
    return Queue(tmp_path / "door.db")
