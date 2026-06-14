"""Tests for the service supervisor: PID journal helpers + the boot-time
reconcile loop with its 10s reconnect window.

Avoids spawning real subprocesses by monkeypatching ``spawn_service`` and
``kill_pid_group``. ``_RECONNECT_WINDOW_S`` is monkeypatched down to a
fraction of a second so the loop completes quickly.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.hub]

import asyncio
import os
import signal
import subprocess
import time
from unittest.mock import MagicMock

import pytest

from awm.gateway.hub import rpc as rpc_mod
from awm.gateway.hub import supervisor


@pytest.fixture(autouse=True)
def _isolate_state(awm_workspace):
    """Each test starts with: clean rpc channel table, clean registry,
    AWM_DIR pointing at the per-test workspace (so the journal file lands
    in tmp_path), and a tiny reconnect window."""
    from awm.gateway.hub import registry as reg_mod
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()
    yield
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()


# ---------------------------------------------------------------------------
# Journal helpers — pure file I/O
# ---------------------------------------------------------------------------


class TestJournalRoundTrip:
    def test_empty_journal_when_no_file(self):
        assert supervisor.load_service_journal() == {}

    def test_write_then_load(self):
        supervisor.write_service_journal({
            "tts": {"name": "tts", "service_id": "abc",
                    "start_cmd": ["start.sh"], "cwd": "/tmp"},
        })
        loaded = supervisor.load_service_journal()
        assert loaded["tts"]["service_id"] == "abc"
        assert loaded["tts"]["start_cmd"] == ["start.sh"]

    def test_update_entry_merges_patch(self):
        supervisor.update_service_journal_entry(
            "ptt", {"service_id": "xyz", "start_cmd": ["go"]},
        )
        supervisor.update_service_journal_entry(
            "ptt", {"last_pid": 4242},
        )
        state = supervisor.load_service_journal()
        assert state["ptt"]["service_id"] == "xyz"
        assert state["ptt"]["start_cmd"] == ["go"]
        assert state["ptt"]["last_pid"] == 4242
        assert state["ptt"]["name"] == "ptt"

    def test_remove_entry(self):
        supervisor.update_service_journal_entry("a", {"x": 1})
        supervisor.update_service_journal_entry("b", {"y": 2})
        supervisor.remove_service_journal_entry("a")
        state = supervisor.load_service_journal()
        assert "a" not in state
        assert state["b"]["y"] == 2

    def test_corrupt_file_returns_empty_dict(self):
        path = supervisor._services_journal_path()
        path.write_text("{not valid json", encoding="utf-8")
        assert supervisor.load_service_journal() == {}


# ---------------------------------------------------------------------------
# kill_pid_group safety
# ---------------------------------------------------------------------------


class TestKillPidGroup:
    def test_zero_pid_noop(self):
        # Must not crash and must not signal anything.
        supervisor.kill_pid_group(0)
        supervisor.kill_pid_group(-1)

    def test_dead_pid_swallowed(self):
        """ProcessLookupError on a reaped PID is normal and must not raise."""
        # Spawn a process that exits immediately, reap it, then signal.
        proc = subprocess.Popen(["true"])
        proc.wait()
        # PID may have been recycled; we accept either ProcessLookupError
        # (no such pgrp) or no-op completion. Either way: no exception.
        supervisor.kill_pid_group(proc.pid)

    def test_reaps_killed_child_no_zombie(self):
        """Killing a service child must REAP it — no ``<defunct>`` zombie left.

        Mirrors how the gateway spawns services: a Popen in its own session
        whose handle is discarded, so only ``kill_pid_group`` can reap it.
        Regression for prod zombies left by ``awm services stop/restart``."""
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        pid = proc.pid
        supervisor.kill_pid_group(pid, grace_s=2.0)
        # Fully reaped → not our child anymore: waitpid raises ChildProcessError
        # (a lingering zombie would instead return (pid, status)).
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        # …and the pid is gone from the table entirely.
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        proc.returncode = -signal.SIGKILL  # mark handled so Popen.__del__ is quiet


# ---------------------------------------------------------------------------
# reconcile_journaled_services — happy path (reconnect within window)
# ---------------------------------------------------------------------------


class TestReconcileHappyPath:
    def test_empty_journal_is_noop(self, monkeypatch):
        # Even with respawn machinery wired, an empty journal returns early.
        spawn = MagicMock()
        monkeypatch.setattr(supervisor, "spawn_service", spawn)
        asyncio.new_event_loop().run_until_complete(
            supervisor.reconcile_journaled_services(),
        )
        spawn.assert_not_called()

    def test_service_reconnects_in_window_no_respawn(self, monkeypatch):
        monkeypatch.setattr(supervisor, "_RECONNECT_WINDOW_S", 0.2)
        spawn = MagicMock()
        kill = MagicMock()
        monkeypatch.setattr(supervisor, "spawn_service", spawn)
        monkeypatch.setattr(supervisor, "kill_pid_group", kill)

        sid = "svc-reconnected"
        supervisor.update_service_journal_entry("ptt", {
            "service_id": sid,
            "prefix": "/svc/ptt",
            "last_pid": 9999,
            "start_cmd": ["start.sh"],
            "cwd": "/tmp",
        })

        # Simulate the service already reconnected by ensuring its
        # control channel exists and is marked ready before reconcile runs.
        ch = rpc_mod.ensure_control(sid)
        ch.set_api({})

        async def go():
            await supervisor.reconcile_journaled_services()

        asyncio.new_event_loop().run_until_complete(go())

        spawn.assert_not_called()
        kill.assert_not_called()
        # Registry was rehydrated with the journaled service_id.
        from awm.gateway.hub.registry import get_registry
        rec = get_registry().get_by_name("service", "ptt")
        assert rec is not None
        assert rec.service_id == sid


# ---------------------------------------------------------------------------
# reconcile_journaled_services — respawn path
# ---------------------------------------------------------------------------


class TestReconcileRespawn:
    def test_silent_service_killed_and_respawned(self, monkeypatch):
        monkeypatch.setattr(supervisor, "_RECONNECT_WINDOW_S", 0.2)
        spawned = []
        killed = []

        def fake_spawn(name, cmd, cwd, env):
            spawned.append({"name": name, "cmd": cmd, "cwd": cwd, "env": env})
            return 12345

        def fake_kill(pid, **kw):
            killed.append(pid)

        monkeypatch.setattr(supervisor, "spawn_service", fake_spawn)
        monkeypatch.setattr(supervisor, "kill_pid_group", fake_kill)

        supervisor.update_service_journal_entry("tts", {
            "service_id": "svc-silent",
            "prefix": "/svc/tts",
            "last_pid": 7777,
            "start_cmd": ["start.sh", "--port", "9000"],
            "cwd": "/srv/tts",
            "hub_url": "http://127.0.0.1:7820",
        })

        async def go():
            await supervisor.reconcile_journaled_services()

        t0 = time.monotonic()
        asyncio.new_event_loop().run_until_complete(go())
        elapsed = time.monotonic() - t0

        # Window plus loop tick; well under the production 10s.
        assert elapsed < 2.0
        assert killed == [7777]
        assert len(spawned) == 1
        sp = spawned[0]
        assert sp["name"] == "tts"
        assert sp["cmd"] == ["start.sh", "--port", "9000"]
        assert sp["cwd"] == "/srv/tts"
        assert sp["env"]["AWM_HUB_URL"] == "http://127.0.0.1:7820"
        assert "AWM_HUB_TOKEN" not in sp["env"]
        assert sp["env"]["AWM_SERVICE_NAME"] == "tts"
        assert sp["env"]["AWM_SERVICE_ID"] == "svc-silent"

        # Journal updated with new PID.
        state = supervisor.load_service_journal()
        assert state["tts"]["last_pid"] == 12345

    def test_no_start_cmd_skipped_silently(self, monkeypatch):
        monkeypatch.setattr(supervisor, "_RECONNECT_WINDOW_S", 0.2)
        spawn = MagicMock()
        kill = MagicMock()
        monkeypatch.setattr(supervisor, "spawn_service", spawn)
        monkeypatch.setattr(supervisor, "kill_pid_group", kill)

        supervisor.update_service_journal_entry("orphan", {
            "service_id": "svc-orphan",
            "prefix": "/svc/orphan",
            "last_pid": 5555,
            # start_cmd omitted intentionally.
        })

        asyncio.new_event_loop().run_until_complete(
            supervisor.reconcile_journaled_services(),
        )

        spawn.assert_not_called()
        kill.assert_not_called()

    def test_silent_without_last_pid_still_respawns(self, monkeypatch):
        """When the prior PID is unknown (first boot after journal entry was
        manually crafted, or kill already happened), respawn still fires —
        we just don't try to SIGTERM anything."""
        monkeypatch.setattr(supervisor, "_RECONNECT_WINDOW_S", 0.2)
        spawn = MagicMock(return_value=999)
        kill = MagicMock()
        monkeypatch.setattr(supervisor, "spawn_service", spawn)
        monkeypatch.setattr(supervisor, "kill_pid_group", kill)

        supervisor.update_service_journal_entry("fresh", {
            "service_id": "svc-fresh",
            "prefix": "/svc/fresh",
            "start_cmd": ["go"],
            "cwd": "",
            # last_pid omitted.
        })

        asyncio.new_event_loop().run_until_complete(
            supervisor.reconcile_journaled_services(),
        )

        kill.assert_not_called()
        spawn.assert_called_once()
        assert spawn.call_args.args[0] == "fresh"
