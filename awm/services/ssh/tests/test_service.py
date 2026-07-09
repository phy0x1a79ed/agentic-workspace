from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from awm.ssh import config as ssh_config
from awm.ssh import service as ssh_service
from awm.ssh.config import (
    KNOWN_HOSTS,
    control_path,
    lock_path,
    resolve_host,
)
from awm.ssh.service import ConnState, SSHService


async def _false(_host):
    return False


async def _true(_host):
    return True


class TestConfig:
    def test_known_hosts_has_expected_entries(self) -> None:
        assert "sockeye" in KNOWN_HOSTS
        assert "fir" in KNOWN_HOSTS
        assert "chamois" in KNOWN_HOSTS
        assert "micb0" in KNOWN_HOSTS

    def test_sockeye_needs_vpn_and_cwl(self) -> None:
        cfg = resolve_host("sockeye")
        assert cfg.needs_vpn
        assert cfg.vpn_profile == "ubc"
        assert cfg.twofa_device == "cwl"
        assert cfg.user == "txyliu"

    def test_fir_needs_no_vpn_and_alliance(self) -> None:
        cfg = resolve_host("fir")
        assert not cfg.needs_vpn
        assert cfg.twofa_device == "alliance"
        assert cfg.user == "phyberos"

    def test_chamois_needs_vpn_no_2fa(self) -> None:
        cfg = resolve_host("chamois")
        assert cfg.needs_vpn
        assert cfg.vpn_profile == "ubc"
        assert cfg.twofa_device == ""
        assert cfg.user == "tliu"

    def test_micb0_needs_vpn_no_2fa(self) -> None:
        cfg = resolve_host("micb0")
        assert cfg.needs_vpn
        assert cfg.vpn_profile == "ubc"
        assert cfg.twofa_device == ""
        assert cfg.user == "tliu"

    def test_sockeye_variants(self) -> None:
        for name in ("sockeye1", "sockeye2", "sockeye3"):
            cfg = resolve_host(name)
            assert cfg.needs_vpn
            assert cfg.twofa_device == "cwl"

    def test_unknown_host_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown host"):
            resolve_host("nosuchhost")

    def test_control_path_format(self) -> None:
        cfg = resolve_host("sockeye")
        path = control_path(cfg)
        assert path.endswith("sockeye_22_txyliu")
        assert "live_connections" in path

    def test_fir_is_guarded_others_are_not(self) -> None:
        # Only MFA-lockout-prone fir carries the direct-ssh guard flag.
        assert resolve_host("fir").guarded is True
        assert resolve_host("sockeye").guarded is False
        assert resolve_host("chamois").guarded is False

    def test_lock_path_format(self) -> None:
        p = lock_path(resolve_host("fir"))
        assert p.endswith("fir.lock")
        assert "awm-ssh-locks" in p


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Redirect lock/live dirs so breaker tests never touch the real ~/.ssh."""
    live = tmp_path / "live"
    locks = tmp_path / "locks"
    live.mkdir()
    locks.mkdir()
    for mod in (ssh_config, ssh_service):
        monkeypatch.setattr(mod, "LIVE_DIR", str(live), raising=False)
        monkeypatch.setattr(mod, "LOCK_DIR", str(locks), raising=False)
    return live, locks


class TestCircuitBreaker:
    async def test_locked_host_refuses_opaquely_without_side_effects(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "prior failure")

        # If the breaker leaks past the hold, these blow up loudly.
        async def _boom_check(_host):
            raise AssertionError("_check_master must not run for a held host")

        async def _boom_call(*a, **k):
            raise AssertionError("no gateway (VPN/2FA) call for a held host")

        monkeypatch.setattr(svc, "_check_master", _boom_check)
        monkeypatch.setattr(ssh_service.gatewayclient, "call", _boom_call)

        result = await svc.connect("fir")
        # Opaque to the caller: no "locked", no mechanism, no unlock hint.
        assert result["status"] == "unavailable"
        assert "unlock" not in result["error"].lower()
        assert "lock" not in result["error"].lower()
        # Hold persists (only a Discord /approve window lifts it).
        assert svc._read_lock(cfg) is not None

    async def test_no_unlock_verb_or_method(self) -> None:
        # Agents must not be able to clear their own hold.
        from awm.ssh import hub_adapter
        assert "unlock" not in hub_adapter.HANDLERS
        verbs = [f["name"] for f in hub_adapter.API_MANIFEST["functions"]]
        assert "unlock" not in verbs
        assert not hasattr(SSHService, "unlock")

    async def test_approve_window_lifts_hold_and_reconnects(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "prior failure")

        # Operator ran /approve for fir's device → window open.
        svc._handle_approve({"command": "approve", "device": cfg.twofa_device})
        assert svc._approve_active(cfg.twofa_device)

        # With the window open, connect clears the hold and (master present)
        # returns connected without minting a fresh auth.
        async def _master_up(_host):
            return True

        monkeypatch.setattr(svc, "_check_master", _master_up)
        result = await svc.connect("fir")
        assert result["status"] == "connected"
        assert svc._read_lock(cfg) is None

    def test_handle_approve_only_matches_device_and_command(
            self, isolated_dirs) -> None:
        svc = SSHService()
        svc._handle_approve({"command": "deny", "device": "alliance"})
        assert not svc._approve_active("alliance")
        svc._handle_approve({"command": "approve"})  # no device
        assert not svc._approve_active("alliance")
        svc._handle_approve({"command": "approve", "device": "alliance"})
        assert svc._approve_active("alliance")
        # A window on one device does not open another.
        assert not svc._approve_active("cwl")

    def test_approve_window_expires(self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        svc._handle_approve({"command": "approve", "device": "alliance"})
        assert svc._approve_active("alliance")
        # Advance past the window.
        t0 = time.monotonic()
        monkeypatch.setattr(ssh_service.time, "monotonic",
                            lambda: t0 + ssh_service._APPROVE_WINDOW_SECONDS + 1)
        assert not svc._approve_active("alliance")

    async def test_trip_breaker_writes_lock_and_alerts(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        alerts: list[str] = []

        async def _capture_alert(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture_alert)
        assert svc._read_lock(cfg) is None
        await svc._trip_breaker(cfg, "ControlMaster did not appear")

        lock = svc._read_lock(cfg)
        assert lock is not None
        assert lock["host"] == "fir"
        assert "ControlMaster" in lock["reason"]
        assert len(alerts) == 1
        assert "fir" in alerts[0]
        # Alert steers the operator to /approve, never a file or a verb.
        assert "/approve" in alerts[0]
        assert "unlock" not in alerts[0].lower()
        assert ".lock" not in alerts[0]

    async def test_alert_never_raises_into_connect(
            self, isolated_dirs, monkeypatch) -> None:
        # A social-service hiccup must not mask the lock.
        svc = SSHService()
        cfg = resolve_host("fir")

        async def _explode(*a, **k):
            raise RuntimeError("social down")

        monkeypatch.setattr(ssh_service.gatewayclient, "call", _explode)
        await svc._trip_breaker(cfg, "boom")  # must not raise
        assert svc._read_lock(cfg) is not None

    def test_failure_reason_includes_askpass_deviation(
            self, isolated_dirs) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        marker = svc._deviation_marker(cfg)
        with open(marker, "w") as f:
            f.write("unrecognized prompt\n")
        reason = svc._failure_reason(cfg, marker, "base failure")
        assert "askpass deviation" in reason


class TestStateMachine:
    """The connect/disconnect state machine: dedup, breaker-safe timeout,
    disconnect-during-auth deferral, and the CONNECTED liveness probe."""

    async def test_concurrent_connects_dedup_to_one_attempt(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        calls = {"n": 0}

        async def _one_attempt(c):
            calls["n"] += 1
            await asyncio.sleep(0.01)  # keep it AUTHENTICATING while peers arrive
            return SSHService._status_dict(c, "connected")

        monkeypatch.setattr(svc, "_check_master", _false)  # no existing master
        monkeypatch.setattr(svc, "_do_connect", _one_attempt)

        results = await asyncio.gather(
            svc.connect("fir"), svc.connect("fir"), svc.connect("fir"))

        assert calls["n"] == 1                              # exactly one ssh auth
        assert all(r["status"] == "connected" for r in results)
        assert svc._host("fir").state == ConnState.CONNECTED

    async def test_attempt_timeout_trips_breaker_and_holds(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        alerts: list[str] = []

        async def _hang(c, marker):        # never brings up the master
            await asyncio.sleep(5.0)

        async def _capture_alert(text):
            alerts.append(text)

        monkeypatch.setattr(ssh_service, "_CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _hang)
        monkeypatch.setattr(svc, "_exit_master", _true)      # reap is a no-op here
        monkeypatch.setattr(svc, "_alert", _capture_alert)

        result = await svc.connect("fir")
        # The internal timeout became an auth FAILURE routed through the breaker —
        # never an outer cancel that skips it.
        assert result["status"] == "error"
        assert svc._read_lock(cfg) is not None
        assert len(alerts) == 1
        assert svc._host("fir").state == ConnState.DISCONNECTED

        # And the very next automated connect is refused without any ssh work.
        monkeypatch.setattr(svc, "_check_master", _false)
        again = await svc.connect("fir")
        assert again["status"] == "unavailable"

    async def test_disconnect_during_auth_defers_then_disposes(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        gate = asyncio.Event()
        completed = {"v": False}

        async def _held_attempt(c):
            await gate.wait()            # stay in AUTHENTICATING until released
            completed["v"] = True
            return SSHService._status_dict(c, "connected")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_do_connect", _held_attempt)
        monkeypatch.setattr(svc, "_exit_master", _true)

        ctask = asyncio.create_task(svc.connect("fir"))
        for _ in range(200):
            await asyncio.sleep(0.001)
            if svc._host("fir").state == ConnState.AUTHENTICATING:
                break
        assert svc._host("fir").state == ConnState.AUTHENTICATING

        dtask = asyncio.create_task(svc.disconnect("fir"))
        await asyncio.sleep(0.02)
        # The in-flight auth is NOT aborted — the disconnect only records intent.
        assert svc._host("fir").pending_disconnect is True
        assert not ctask.done()

        gate.set()
        cres = await ctask
        dres = await dtask
        assert cres["status"] == "connected"
        assert completed["v"] is True            # attempt ran to completion
        assert dres["status"] == "disconnected"
        assert svc._host("fir").state == ConnState.DISCONNECTED

    async def test_connected_probe_no_new_attempt_when_live(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        svc._host("fir").state = ConnState.CONNECTED

        async def _no_attempt(c):
            raise AssertionError("must not re-authenticate a live master")

        monkeypatch.setattr(svc, "_check_master", _true)
        monkeypatch.setattr(svc, "_do_connect", _no_attempt)
        result = await svc.connect("fir")
        assert result["status"] == "connected"

    async def test_connected_probe_reauths_when_master_dead(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        svc._host("fir").state = ConnState.CONNECTED
        calls = {"n": 0}

        async def _reconnect(c):
            calls["n"] += 1
            return SSHService._status_dict(c, "connected")

        monkeypatch.setattr(svc, "_check_master", _false)   # master died out-of-band
        monkeypatch.setattr(svc, "_do_connect", _reconnect)
        result = await svc.connect("fir")
        assert result["status"] == "connected"
        assert calls["n"] == 1                              # demoted → fresh auth
        assert svc._host("fir").state == ConnState.CONNECTED


class TestSingletonAndReconcile:
    @pytest.fixture
    def isolated_singleton(self, tmp_path, monkeypatch):
        path = str(tmp_path / "awm-ssh.singleton")
        for mod in (ssh_config, ssh_service):
            monkeypatch.setattr(mod, "SINGLETON_PATH", path, raising=False)
        return path

    def test_second_instance_stands_down(
            self, isolated_singleton, monkeypatch) -> None:
        first = SSHService()
        first._acquire_singleton()
        assert first._singleton_fd is not None      # holds the flock

        class _Stood(Exception):
            pass

        def _fake_exit(code):
            raise _Stood()

        monkeypatch.setattr(ssh_service.os, "_exit", _fake_exit)
        second = SSHService()
        with pytest.raises(_Stood):
            second._acquire_singleton()             # contended → stands down

    async def test_reconcile_adopts_live_master(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()

        async def _only_fir_up(host):
            return host == "fir"

        monkeypatch.setattr(svc, "_check_master", _only_fir_up)
        await svc._reconcile_on_boot()
        assert svc._host("fir").state == ConnState.CONNECTED
        assert svc._host("sockeye").state == ConnState.DISCONNECTED

    async def test_reap_removes_dead_socket_keeps_regular_files(
            self, isolated_dirs, monkeypatch) -> None:
        live, _locks = isolated_dirs
        svc = SSHService()

        # A real unix socket (S_ISSOCK) standing in for a stale master, plus a
        # regular stderr file that must be left alone.
        sockpath = live / "fir.computecanada.ca_22_phyberos"
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(sockpath))
        stderr_file = live / "fir.connect.stderr"
        stderr_file.write_text("some stderr")

        monkeypatch.setattr(svc, "_check_socket", _false)  # socket is dead
        try:
            await svc._reap_orphans()
        finally:
            s.close()

        assert not sockpath.exists()        # stale socket reaped
        assert stderr_file.exists()         # regular file untouched


_HOME = Path(os.path.expanduser("~"))
_GUARD = _HOME / ".ssh" / "awm-ssh-guard"
_ASKPASS = _HOME / ".ssh" / "awm-duo-askpass"


class TestGuardScripts:
    """Behavioural checks on the installed shell helpers (skip if absent)."""

    @pytest.mark.skipif(not _GUARD.exists(), reason="guard not installed")
    def test_guard_exits_nonzero_and_is_loud(self) -> None:
        r = subprocess.run([str(_GUARD), "fir", "22"], capture_output=True,
                           text=True)
        assert r.returncode != 0
        assert "BLOCKED" in r.stderr

    @pytest.mark.skipif(not _ASKPASS.exists(), reason="askpass not installed")
    def test_askpass_answers_matching_device(self) -> None:
        prompt = ("Duo two-factor login\n\n 1. Duo Push to Mira\n"
                  " 2. Phone call\n\nPasscode or option (1-2): ")
        r = subprocess.run([str(_ASKPASS), prompt], capture_output=True,
                           text=True)
        assert r.returncode == 0
        assert r.stdout.strip() == "1"

    @pytest.mark.skipif(not _ASKPASS.exists(), reason="askpass not installed")
    def test_askpass_refuses_unrecognized_prompt(self, tmp_path) -> None:
        marker = tmp_path / "mk"
        r = subprocess.run([str(_ASKPASS), "password: "],
                           capture_output=True, text=True,
                           env={**os.environ,
                                "AWM_SSH_ASKPASS_MARKER": str(marker)})
        assert r.returncode != 0
        assert r.stdout.strip() == ""
        assert marker.exists()

    @pytest.mark.skipif(not _ASKPASS.exists(), reason="askpass not installed")
    def test_askpass_refuses_when_no_device_matches(self, tmp_path) -> None:
        marker = tmp_path / "mk"
        prompt = ("Enter a passcode or select one of the following options:\n"
                  " 1. Duo Push to SomeoneElse\n 2. Phone call\n"
                  "Passcode or option (1-2): ")
        r = subprocess.run([str(_ASKPASS), prompt], capture_output=True,
                           text=True,
                           env={**os.environ,
                                "AWM_SSH_ASKPASS_MARKER": str(marker)})
        assert r.returncode != 0
        assert r.stdout.strip() == ""
        assert marker.exists()
