"""Test bootstrap for the agents dist.

The per-dist test runner (``awm/gateway/scripts/run-tests.sh``) puts only this
dist's source root + the shared components (config / persistence /
gatewayclient) on ``PYTHONPATH``. The agents service now imports
``awm.agentcore`` (the harness layer), which is a leaf component living under
``awm/service_components/agentcore`` — NOT in that component set. Add its source
root here so ``import awm.agentcore`` resolves under the agents dist's
interpreter.

This is import-path-only (no namespace shadowing risk): agentcore ships under
the same PEP 420 ``awm`` namespace and provides only the ``awm.agentcore``
subpackage, which the agents source root does not also provide.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_AGENTCORE_SRC = (
    Path(__file__).resolve().parents[4]
    / "service_components" / "agentcore"
)
if _AGENTCORE_SRC.is_dir():
    p = str(_AGENTCORE_SRC)
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Shared fixtures for the task-bounded placement tests (T2)
# ---------------------------------------------------------------------------

@pytest.fixture
def agents_env(tmp_path, monkeypatch):
    """Tmp AWM_DIR + PROJECTS_DIR + a fresh agents.db + a recording gateway stub.

    Yields ``{projects_dir, awm_dir, calls}`` where ``calls`` is a list of
    ``(service, fn, args)`` recorded from every ``gatewayclient.call`` — so a
    test can assert ordering (e.g. ``scope_create`` before spawn, ``deliver``
    before ``scope_complete``). ``scope_create`` also makes the worktree dir so
    the spawn path's existence check passes. No live hub is touched."""
    import awm.agents.agent_instances as ai_mod
    import awm.agents.dao as dao_mod
    import awm.persistence.databases as dbs_mod
    import awm.gatewayclient as gw
    import awm.config as _cfg

    awm_dir = tmp_path / ".awm"
    awm_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(ai_mod, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(_cfg, "AWM_DIR", awm_dir)
    monkeypatch.setattr(dbs_mod, "SERVICES_DIR", awm_dir / "services")

    calls: list = []

    async def _fake_call(service, fn, args=None, *, as_=None, **kw):
        a = args or {}
        calls.append((service, fn, a))
        if fn == "scope_create":
            ws = projects_dir / a["project"] / a["scope"]
            ws.mkdir(parents=True, exist_ok=True)
        return {"ok": True}

    monkeypatch.setattr(gw, "call", _fake_call)

    dao_mod._initialized = False
    ai_mod._dao = None
    from awm.agents.dao import init as dao_init
    dao_init()

    yield {"projects_dir": projects_dir, "awm_dir": awm_dir, "calls": calls}

    dao_mod._initialized = False
    ai_mod._dao = None


class _FakeProc:
    """Subprocess stand-in whose wait() resolves on terminate()/kill().

    Unlike a never-returning stub, this lets the waiter loop tear the session
    down so respawn (stop → wait-for-deregister → spawn) completes in tests."""

    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode = None
        self._exit = asyncio.Event()

    async def wait(self):
        await self._exit.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self):
        self.returncode = -15
        self._exit.set()

    def kill(self):
        self.returncode = -9
        self._exit.set()


class _FakeCoreSession:
    """Stand-in for an agentcore AgentSession (empty event stream)."""

    def __init__(self):
        self.config = None
        self.sent: list[str] = []
        self._proc = _FakeProc()

    def subscribe(self):
        async def _gen():
            if False:
                yield  # pragma: no cover — empty async iterator
        return _gen()

    async def start(self):
        pass

    async def send(self, text):
        self.sent.append(text)

    async def close(self):
        pass


@pytest.fixture
def stub_core(monkeypatch):
    """Patch open_agent to return a controllable fake session.

    Yields ``{session, opened}`` — ``opened`` is the list of AgentConfigs in
    spawn order (so a test can assert the spawn happened, and when)."""
    import awm.agents.agent_instances as ai_mod

    captured: dict = {"opened": []}

    def fake_open_agent(config):
        sess = _FakeCoreSession()
        sess.config = config
        captured["session"] = sess
        captured["opened"].append(config)
        return sess

    monkeypatch.setattr(ai_mod, "open_agent", fake_open_agent)
    ai_mod._registry_by_id.clear()
    ai_mod._by_scope.clear()
    yield captured
    ai_mod._registry_by_id.clear()
    ai_mod._by_scope.clear()
