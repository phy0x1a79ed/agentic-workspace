"""Runtime crash-respawn watchdog tests (lifecycle plan T5).

``supervise_disconnect`` re-registers a disconnected service, waits the
reconnect window, and respawns it from the journal if it stays silent — but
only when it is still journaled, enabled, not reconnected, and the gateway is
not shutting down.

Real subprocesses are avoided by monkeypatching ``spawn_service`` /
``kill_pid_group``; ``_RECONNECT_WINDOW_S`` is shrunk so the wait is sub-second.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.hub]

import asyncio
from unittest.mock import MagicMock

from awm.gateway.hub import discovery as disc_mod
from awm.gateway.hub import registry as reg_mod
from awm.gateway.hub import rpc as rpc_mod
from awm.gateway.hub import supervisor


@pytest.fixture(autouse=True)
def _isolate(awm_workspace, monkeypatch):
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()
    supervisor.set_shutting_down(False)
    supervisor.reset_breaker_state()
    monkeypatch.setattr(supervisor, "_RECONNECT_WINDOW_S", 0.1)
    yield
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()
    supervisor.set_shutting_down(False)
    supervisor.reset_breaker_state()


def _journal(name="tts", **extra):
    entry = {
        "service_id": f"svc-{name}",
        "prefix": f"/svc/{name}",
        "last_pid": 7777,
        "start_cmd": ["bash", "run.sh"],
        "cwd": "/srv",
    }
    entry.update(extra)
    supervisor.update_service_journal_entry(name, entry)


def test_silent_journaled_service_respawned(monkeypatch):
    spawn = MagicMock(return_value=12345)
    kill = MagicMock()
    monkeypatch.setattr(supervisor, "spawn_service", spawn)
    monkeypatch.setattr(supervisor, "kill_pid_group", kill)
    _journal("tts")

    asyncio.run(supervisor.supervise_disconnect("tts"))

    spawn.assert_called_once()
    assert spawn.call_args.args[0] == "tts"
    # Journal updated with the fresh PID.
    assert supervisor.load_service_journal()["tts"]["last_pid"] == 12345


def test_dejournaled_service_not_respawned(monkeypatch):
    """A deliberate `awm services stop` drops the journal entry before the kill,
    so the watchdog sees nothing to respawn."""
    spawn = MagicMock(return_value=1)
    monkeypatch.setattr(supervisor, "spawn_service", spawn)
    monkeypatch.setattr(supervisor, "kill_pid_group", MagicMock())
    # No journal entry for "ghost".

    asyncio.run(supervisor.supervise_disconnect("ghost"))
    spawn.assert_not_called()


def test_shutting_down_skips_respawn(monkeypatch):
    spawn = MagicMock(return_value=1)
    monkeypatch.setattr(supervisor, "spawn_service", spawn)
    monkeypatch.setattr(supervisor, "kill_pid_group", MagicMock())
    _journal("tts")
    supervisor.set_shutting_down(True)

    asyncio.run(supervisor.supervise_disconnect("tts"))
    spawn.assert_not_called()


def test_disabled_service_not_respawned(monkeypatch):
    spawn = MagicMock(return_value=1)
    monkeypatch.setattr(supervisor, "spawn_service", spawn)
    monkeypatch.setattr(supervisor, "kill_pid_group", MagicMock())
    _journal("tts")
    disc_mod.set_enabled("tts", False)

    asyncio.run(supervisor.supervise_disconnect("tts"))
    spawn.assert_not_called()


def test_reconnected_service_not_respawned(monkeypatch):
    """If the service reopens a ready control channel within the window, the
    watchdog leaves it alone."""
    spawn = MagicMock(return_value=1)
    monkeypatch.setattr(supervisor, "spawn_service", spawn)
    monkeypatch.setattr(supervisor, "kill_pid_group", MagicMock())
    _journal("tts")
    # Simulate the service's quick reconnect: a ready control channel on its sid.
    ch = rpc_mod.ensure_control("svc-tts")
    ch.set_api({})

    asyncio.run(supervisor.supervise_disconnect("tts"))
    spawn.assert_not_called()
