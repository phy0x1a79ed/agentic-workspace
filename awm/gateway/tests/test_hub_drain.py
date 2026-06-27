"""Graceful in-band drain tests (lifecycle T4 — the in-band stand-down path).

``server._drain_services`` is the single graceful-stop coroutine. On a graceful
gateway stop it sends each live, non-overlay service a ``{"kind":"shutdown"}``
frame over its own control channel, waits for the service to drop its lease
(exit itself), force-kills only a straggler that ignores the frame, and clears
the journal.

These tests drive the coroutine directly (the signal override that calls it on
Linux can't run under pytest) with a real registry record + held lease + control
channel, and assert:

* the shutdown frame is enqueued on the live channel and the journal is cleared;
* ``kill_pid_group`` is NOT called when the lease drops promptly (the frame did
  the work);
* ``kill_pid_group`` IS called for a straggler whose lease never drops.

No real subprocess is spawned; the grace window is shrunk to keep it sub-second.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.hub]

import asyncio
from unittest.mock import MagicMock

from awm.gateway import server as srv
from awm.gateway.hub import lease as lease_mod
from awm.gateway.hub import registry as reg_mod
from awm.gateway.hub import rpc as rpc_mod
from awm.gateway.hub import supervisor


@pytest.fixture(autouse=True)
def _isolate(awm_workspace, monkeypatch):
    # Fresh registry + lease manager bound to it + empty channel table.
    reg_mod._singleton = reg_mod.Registry()
    lease_mod._singleton = None
    rpc_mod._channels.clear()
    supervisor.set_shutting_down(False)
    monkeypatch.setattr(srv, "_GRACE_DEADLINE_S", 0.1)
    yield
    reg_mod._singleton = reg_mod.Registry()
    lease_mod._singleton = None
    rpc_mod._channels.clear()
    supervisor.set_shutting_down(False)


async def _make_live_service(name="tts", pid=7777):
    """Register a service, journal it, give it a ready control channel and a
    held lease — i.e. exactly what the drain treats as a live service. Returns
    (rec, control_channel, disconnect_event, hold_task)."""
    registry = reg_mod.get_registry()
    rec = await registry.register_service(
        name, f"/svc/{name}", pid=pid, start_cmd=["bash", "run.sh"], cwd="/srv",
    )
    supervisor.update_service_journal_entry(name, {
        "service_id": rec.service_id,
        "prefix": f"/svc/{name}",
        "last_pid": pid,
        "start_cmd": ["bash", "run.sh"],
        "cwd": "/srv",
    })
    ch = rpc_mod.ensure_control(rec.service_id)
    ch.set_api({})  # mark ready

    lm = lease_mod.get_lease_manager()
    disconnect = asyncio.Event()
    hold_task = asyncio.create_task(lm.hold(rec.service_id, disconnect))
    await asyncio.sleep(0)  # let hold() register the holder
    assert lm.is_held(rec.service_id)
    return rec, ch, disconnect, hold_task


async def test_drain_enqueues_frame_and_clears_journal(monkeypatch):
    """The drain enqueues a {"kind":"shutdown"} frame on the live channel and
    clears the journal; force-kill is NOT used when the lease drops promptly."""
    kill = MagicMock()
    monkeypatch.setattr(supervisor, "kill_pid_group", kill)
    # The (fake) service "process" has exited along with its lease.
    monkeypatch.setattr(srv, "_pid_alive", lambda pid: False)

    rec, ch, disconnect, hold_task = await _make_live_service("tts")

    # Spy on the frame and simulate the service standing itself down on receipt
    # (its lease drops — the in-band path doing its job).
    enqueued: list[dict] = []
    orig_enqueue = ch.enqueue

    def _spy(env):
        enqueued.append(env)
        disconnect.set()  # service exits → lease will drop on the next tick
        orig_enqueue(env)

    monkeypatch.setattr(ch, "enqueue", _spy)

    await srv._drain_services()
    await asyncio.sleep(0)
    hold_task.cancel()

    assert {"kind": "shutdown"} in enqueued
    assert supervisor.load_service_journal() == {}
    kill.assert_not_called()  # frame did the work; no force-kill


async def test_drain_force_kills_straggler(monkeypatch):
    """A service that ignores the frame (lease never drops) is force-killed
    within the grace window, and the journal is still cleared."""
    kill = MagicMock()
    monkeypatch.setattr(supervisor, "kill_pid_group", kill)
    # Isolate the lease-held signal: the kill must fire from the held lease,
    # not from a pid-alive coincidence.
    monkeypatch.setattr(srv, "_pid_alive", lambda pid: False)

    rec, ch, disconnect, hold_task = await _make_live_service("tts", pid=4242)
    # Note: we never set `disconnect`, so the lease stays held — a wedged
    # service that ignored the shutdown frame.

    await srv._drain_services()

    kill.assert_called_once_with(4242)
    assert supervisor.load_service_journal() == {}

    disconnect.set()
    await asyncio.sleep(0)
    hold_task.cancel()


async def test_drain_force_kills_live_orphan_in_backstop(monkeypatch):
    """Backstop path: the lease was already released (uvicorn closed the WS
    before the drain ran) but the service process is still alive. The drain
    must still force-kill it — lease-held alone would miss it."""
    kill = MagicMock()
    monkeypatch.setattr(supervisor, "kill_pid_group", kill)
    monkeypatch.setattr(srv, "_pid_alive", lambda pid: True)  # process still up

    rec, ch, disconnect, hold_task = await _make_live_service("tts", pid=9191)
    disconnect.set()           # lease already gone (WS force-closed)
    await asyncio.sleep(0)
    assert not lease_mod.get_lease_manager().is_held(rec.service_id)

    await srv._drain_services()

    kill.assert_called_once_with(9191)
    assert supervisor.load_service_journal() == {}
    hold_task.cancel()


async def test_drain_is_idempotent(monkeypatch):
    """A second drain finds no live leases and an empty journal — a no-op."""
    kill = MagicMock()
    monkeypatch.setattr(supervisor, "kill_pid_group", kill)
    monkeypatch.setattr(srv, "_pid_alive", lambda pid: False)

    rec, ch, disconnect, hold_task = await _make_live_service("tts")
    disconnect.set()  # service already gone
    await asyncio.sleep(0)

    await srv._drain_services()
    kill.reset_mock()
    # Second call: nothing live, journal already empty.
    await srv._drain_services()

    kill.assert_not_called()
    assert supervisor.load_service_journal() == {}
    hold_task.cancel()


async def test_drain_skips_overlay(monkeypatch):
    """An overlay belongs to a live `awm dev shadow` process — the drain must
    not signal or kill it."""
    kill = MagicMock()
    monkeypatch.setattr(supervisor, "kill_pid_group", kill)

    registry = reg_mod.get_registry()
    # Base service so an overlay can be pushed onto the prefix.
    base = await registry.register_service(
        "tts", "/svc/tts", pid=1, start_cmd=["bash", "run.sh"], cwd="/srv",
    )
    overlay = reg_mod.ServiceRecord(
        name="tts-shadow", prefix="/svc/tts", kind="service",
        start_cmd=["bash", "run.sh"], cwd="/srv", backend_pid=999,
    )
    await registry.replace_overlays(overlay)

    ch = rpc_mod.ensure_control(overlay.service_id)
    ch.set_api({})
    enqueued: list[dict] = []
    monkeypatch.setattr(ch, "enqueue", enqueued.append)

    lm = lease_mod.get_lease_manager()
    disconnect = asyncio.Event()
    hold_task = asyncio.create_task(lm.hold(overlay.service_id, disconnect))
    await asyncio.sleep(0)

    await srv._drain_services()

    assert enqueued == []          # overlay never signalled
    kill.assert_not_called()       # overlay never killed

    disconnect.set()
    await asyncio.sleep(0)
    hold_task.cancel()
