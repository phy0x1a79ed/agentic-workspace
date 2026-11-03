"""Shared fixtures: an isolated environment and a queue in a temp dir."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from awm.reporting.store import Queue  # noqa: E402


@pytest.fixture(autouse=True)
def door_env(monkeypatch, tmp_path):
    """A node absent from an empty fleet, the door switched on and every path in tmp."""
    monkeypatch.setenv("AWM_FRONT_DOOR", "1")
    monkeypatch.setenv("AWM_NODE_NAME", "testnode")
    monkeypatch.setenv("AWM_EDGES_FILE", str(tmp_path / "edges.yml"))
    monkeypatch.setenv("AWM_REPORTING_DB", str(tmp_path / "reporting.db"))
    monkeypatch.setenv("AWM_REPORTING_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    (tmp_path / "awm-admin-approve").touch()
    monkeypatch.setenv("AWM_ADMIN_APPROVE", str(tmp_path / "awm-admin-approve"))
    for name in ("AWM_BOARD_URL", "AWM_BOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_live_claude(monkeypatch, tmp_path):
    """Nothing here may read a real Claude Code job."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


@pytest.fixture
def fleet(tmp_path):
    """Write the edge list: ``nodes`` is {name: role}, ``edges`` is {src: [dst, ...]}."""
    import yaml

    def write(nodes: dict[str, str], edges: dict[str, list[str]] | None = None) -> None:
        (tmp_path / "edges.yml").write_text(yaml.safe_dump({
            "nodes": {n: {"role": r} for n, r in nodes.items()},
            "edges": {s: {d: "lan" for d in ds} for s, ds in (edges or {}).items()},
        }))

    return write


@pytest.fixture
def queue(tmp_path):
    return Queue(tmp_path / "reporting.db")
