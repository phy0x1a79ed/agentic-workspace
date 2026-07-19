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
def _isolate_state(awm_workspace, tmp_path, monkeypatch):
    """Each test starts with: clean rpc channel table, clean registry,
    AWM_DIR pointing at the per-test workspace (so the journal file lands
    in tmp_path), and a tiny reconnect window.

    ``AWM_SERVICES_DIR`` / ``AWM_PAGES_DIR`` are pinned at empty temp dirs so
    the journal-based tests see *no* discoverable service (their identity comes
    purely from the journal — the pre-L2 fallback path). Tests that exercise the
    L2 discovery-wins behaviour set ``AWM_SERVICES_DIR`` to a populated tree of
    their own (a later ``setenv`` wins)."""
    from awm.gateway.hub import registry as reg_mod
    (tmp_path / "_empty_services").mkdir()
    (tmp_path / "_empty_pages").mkdir()
    monkeypatch.setenv("AWM_SERVICES_DIR", str(tmp_path / "_empty_services"))
    monkeypatch.setenv("AWM_PAGES_DIR", str(tmp_path / "_empty_pages"))
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()
    yield
    reg_mod._singleton = reg_mod.Registry()
    rpc_mod._channels.clear()


def _make_service_folder(root, name, cwd_marker=True):
    """Create a discoverable service folder (``<root>/<name>/run.sh``)."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "run.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    return folder


def _make_built_page(root, name, prefix=None):
    """Create a discoverable, servable page bundle (``<root>/<name>/dist/``)."""
    pkg = root / name
    (pkg / "dist").mkdir(parents=True)
    (pkg / "dist" / "index.html").write_text("<html>built</html>", encoding="utf-8")
    if prefix is not None:
        (pkg / "prefix.txt").write_text(prefix, encoding="utf-8")
    return pkg


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
            "stt", {"service_id": "xyz", "start_cmd": ["go"]},
        )
        supervisor.update_service_journal_entry(
            "stt", {"last_pid": 4242},
        )
        state = supervisor.load_service_journal()
        assert state["stt"]["service_id"] == "xyz"
        assert state["stt"]["start_cmd"] == ["go"]
        assert state["stt"]["last_pid"] == 4242
        assert state["stt"]["name"] == "stt"

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
        supervisor.update_service_journal_entry("stt", {
            "service_id": sid,
            "prefix": "/svc/stt",
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
        rec = get_registry().get_by_name("service", "stt")
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


# ---------------------------------------------------------------------------
# L2: filesystem-derived identity — discovery wins over the journal
# ---------------------------------------------------------------------------


class TestResolveIdentity:
    def test_discovery_overrides_journal_cwd_and_cmd(self, tmp_path, monkeypatch):
        """A journaled service whose entry names a *wrong* cwd (the prod
        feat-federation contamination) respawns from the DISCOVERED cwd/cmd."""
        svc_root = tmp_path / "svcs"
        folder = _make_service_folder(svc_root, "tts")
        monkeypatch.setenv("AWM_SERVICES_DIR", str(svc_root))

        entry = {
            "service_id": "svc-x",
            "start_cmd": ["stale.sh", "--wrong"],
            "cwd": "/some/feat-federation/worktree/awm/services/tts",
        }
        cmd, cwd = supervisor._resolve_identity("tts", entry)
        assert cmd == ["bash", "run.sh"]
        assert cwd == str(folder)

    def test_non_discoverable_falls_back_to_journal(self, tmp_path, monkeypatch):
        """An external (over-the-wire) registration with no folder here keeps
        its journaled identity."""
        monkeypatch.setenv("AWM_SERVICES_DIR", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()
        entry = {"start_cmd": ["remote.sh"], "cwd": "/remote/cwd"}
        cmd, cwd = supervisor._resolve_identity("external", entry)
        assert cmd == ["remote.sh"]
        assert cwd == "/remote/cwd"

    def test_discoverable_empty_start_cmd_still_respawns(self, tmp_path, monkeypatch):
        """The silent-death bug: a discoverable service whose journal
        ``start_cmd`` was clobbered to empty still respawns (from discovery),
        instead of being skipped."""
        monkeypatch.setattr(supervisor, "_RECONNECT_WINDOW_S", 0.2)
        svc_root = tmp_path / "svcs"
        folder = _make_service_folder(svc_root, "stt")
        monkeypatch.setenv("AWM_SERVICES_DIR", str(svc_root))

        spawned = []
        monkeypatch.setattr(supervisor, "spawn_service",
                            lambda name, cmd, cwd, env: spawned.append((name, cmd, cwd)) or 4242)
        monkeypatch.setattr(supervisor, "kill_pid_group", MagicMock())

        supervisor.update_service_journal_entry("stt", {
            "service_id": "svc-stt",
            "prefix": "/svc/stt",
            "last_pid": 111,
            # start_cmd clobbered to empty by a bad self-register.
            "start_cmd": [],
            "cwd": "",
        })

        asyncio.new_event_loop().run_until_complete(
            supervisor.reconcile_journaled_services())

        assert len(spawned) == 1
        name, cmd, cwd = spawned[0]
        assert name == "stt"
        assert cmd == ["bash", "run.sh"]
        assert cwd == str(folder)

    def test_respawn_rewrites_journal_to_discovered_identity(self, tmp_path, monkeypatch):
        """Self-heal: after respawn, the journal entry is corrected to the
        discovered cwd/start_cmd — retiring the manual ``rm services.json``."""
        monkeypatch.setattr(supervisor, "_RECONNECT_WINDOW_S", 0.2)
        svc_root = tmp_path / "svcs"
        folder = _make_service_folder(svc_root, "agents")
        monkeypatch.setenv("AWM_SERVICES_DIR", str(svc_root))
        monkeypatch.setattr(supervisor, "spawn_service",
                            lambda name, cmd, cwd, env: 5151)
        monkeypatch.setattr(supervisor, "kill_pid_group", MagicMock())

        supervisor.update_service_journal_entry("agents", {
            "service_id": "svc-a",
            "prefix": "/svc/agents",
            "last_pid": 222,
            "start_cmd": ["stale.sh"],
            "cwd": "/wrong/tree",
        })

        asyncio.new_event_loop().run_until_complete(
            supervisor.reconcile_journaled_services())

        state = supervisor.load_service_journal()
        assert state["agents"]["last_pid"] == 5151
        assert state["agents"]["start_cmd"] == ["bash", "run.sh"]
        assert state["agents"]["cwd"] == str(folder)


# ---------------------------------------------------------------------------
# L1: pages discovered + re-derived on every boot
# ---------------------------------------------------------------------------


class TestBootstrapPages:
    def test_pages_registered_from_filesystem(self, tmp_path, monkeypatch):
        pages_root = tmp_path / "pages"
        _make_built_page(pages_root, "fleet")
        _make_built_page(pages_root, "notes", prefix="/ui/notes")
        _make_built_page(pages_root, "sourceonly")  # has dist → servable
        # A page with no dist is skipped.
        (pages_root / "unbuilt").mkdir()
        monkeypatch.setenv("AWM_PAGES_DIR", str(pages_root))

        from awm.gateway.hub.registry import get_registry
        asyncio.new_event_loop().run_until_complete(
            supervisor.bootstrap_discovered_pages())
        reg = get_registry()
        assert reg.longest_match("/ui/fleet") is not None
        assert reg.longest_match("/ui/notes") is not None
        assert reg.get_by_name("page", "unbuilt") is None

    def test_pages_survive_simulated_restart(self, tmp_path, monkeypatch):
        """The core L1 guarantee: a page base is in-RAM only, but a *second*
        boot (fresh registry) re-derives it from disk — so /ui/<name> survives
        a gateway restart with no manual re-register."""
        pages_root = tmp_path / "pages"
        _make_built_page(pages_root, "fleet")
        monkeypatch.setenv("AWM_PAGES_DIR", str(pages_root))

        from awm.gateway.hub import registry as reg_mod

        # Boot 1
        asyncio.new_event_loop().run_until_complete(
            supervisor.bootstrap_discovered_pages())
        assert reg_mod.get_registry().longest_match("/ui/fleet") is not None

        # Simulate a restart: the in-RAM registry is wiped.
        reg_mod._singleton = reg_mod.Registry()
        assert reg_mod.get_registry().longest_match("/ui/fleet") is None

        # Boot 2 — the page comes back on its own, no HTTP POST.
        asyncio.new_event_loop().run_until_complete(
            supervisor.bootstrap_discovered_pages())
        assert reg_mod.get_registry().longest_match("/ui/fleet") is not None

    def test_prefix_conflict_skips_not_aborts(self, tmp_path, monkeypatch):
        """A page whose prefix is already owned by a different name is logged
        and skipped; the rest of the loop still registers."""
        pages_root = tmp_path / "pages"
        _make_built_page(pages_root, "aaa", prefix="/ui/shared")
        _make_built_page(pages_root, "bbb", prefix="/ui/shared")  # collides
        _make_built_page(pages_root, "ccc")  # independent
        monkeypatch.setenv("AWM_PAGES_DIR", str(pages_root))

        from awm.gateway.hub.registry import get_registry
        asyncio.new_event_loop().run_until_complete(
            supervisor.bootstrap_discovered_pages())
        reg = get_registry()
        # First (sorted) wins the shared prefix; the collider is skipped.
        assert reg.longest_match("/ui/shared").name == "aaa"
        # The independent page still registered despite the collision.
        assert reg.longest_match("/ui/ccc") is not None
