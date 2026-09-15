"""Fixtures for the rlm-chess tests: a throwaway arena and service DB, real arena daemons.

Run with CHESS_PYTHON set to the chess env interpreter and the arena importable.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

# Before any import of game_master: the arena reads its directories once.
_ROOT = tempfile.mkdtemp(prefix="rc-", dir="/tmp")
os.environ["CHESS_ARENA_DATA"] = f"{_ROOT}/data"
os.environ["CHESS_ARENA_RUNTIME"] = f"{_ROOT}/run"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_ROOT, ignore_errors=True)


class FakeAdapter:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    async def emit(self, topic: str, payload: dict) -> None:
        assert topic == "chess"
        self.emitted.append(payload)

    def kinds(self, sid: str) -> list[str]:
        return [e["kind"] for e in self.emitted if e["session_id"] == sid]


@pytest.fixture()
async def realm(tmp_path, monkeypatch):
    """The hub adapter module, with its DB under tmp_path and emits captured."""
    awm_dir = tmp_path / ".awm"
    services_dir = awm_dir / "services"
    services_dir.mkdir(parents=True)
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)

    from awm.rlm_chess import dao, hub_adapter
    monkeypatch.setattr(dao, "_initialized", False)
    dao.init()
    adapter = FakeAdapter()
    monkeypatch.setattr(hub_adapter, "_ADAPTER", adapter)
    hub_adapter.fake = adapter
    yield hub_adapter

    for sid in hub_adapter.sessions.live_sessions():
        await hub_adapter.sessions.kill(sid)
    for task in list(hub_adapter._PUMPS.values()):
        task.cancel()
    hub_adapter._PUMPS.clear()
