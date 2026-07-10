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


async def _noop_alert(_text):
    return None


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

    async def test_approve_window_is_one_shot(
            self, isolated_dirs, monkeypatch) -> None:
        # A single operator /approve authorises exactly ONE reconnect attempt. If
        # that attempt fails and re-trips the breaker, the window must already be
        # consumed — otherwise a caller retrying a persistently-failing connect
        # would re-clear and re-fire a Duo push every iteration, an unbounded run
        # straight to the provider lockout the breaker exists to prevent.
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "prior failure")
        svc._handle_approve({"command": "approve", "device": cfg.twofa_device})
        assert svc._approve_active(cfg.twofa_device)

        async def _fail(c):
            await svc._trip_breaker(c, "still broken")   # re-trips within the attempt
            return SSHService._status_dict(c, "error", error="failed")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_do_connect", _fail)
        monkeypatch.setattr(svc, "_alert", _noop_alert)

        first = await svc.connect("fir")
        assert first["status"] == "error"
        # Window consumed the moment it was spent — not still open for its full
        # duration.
        assert not svc._approve_active(cfg.twofa_device)
        # So the very next automated connect is refused with no ssh work.
        again = await svc.connect("fir")
        assert again["status"] == "unavailable"

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

    async def test_attempt_always_arms_burst_regardless_of_active(
            self, isolated_dirs, monkeypatch) -> None:
        # Each connect fires exactly one Duo push, so _attempt_master must ALWAYS
        # arm +1 on the device budget — never skip because a burst already looks
        # 'active'. Skipping under-grants overlapping same-device connects, holding
        # the 2nd login until it times out (the original overlapping-login bug).
        svc = SSHService()
        cfg = resolve_host("fir")
        calls: list[tuple] = []

        async def _fake_call(service, verb, args=None):
            calls.append((service, verb, args))
            # Pretend a burst is already live — the old code would skip arming.
            return {"status": "ok", "burst_active": True}

        class _Proc:
            async def wait(self):
                return 0

        async def _spawn(*a, **k):
            return _Proc()

        monkeypatch.setattr(ssh_service.gatewayclient, "call", _fake_call)
        monkeypatch.setattr(ssh_service.asyncio, "create_subprocess_exec", _spawn)
        monkeypatch.setattr(svc, "_check_master", _true)    # master appears at once

        await svc._attempt_master(cfg, svc._deviation_marker(cfg))

        burst_calls = [c for c in calls if c[1] == "burst"]
        assert len(burst_calls) == 1
        assert burst_calls[0][2]["count"] == 1
        assert burst_calls[0][2]["device"] == "alliance"
        # And no 'status' pre-check gate remains.
        assert not any(c[1] == "status" for c in calls)

    async def test_attempt_caps_kbd_interactive_to_one_push(
            self, isolated_dirs, monkeypatch) -> None:
        # THE lockout amplifier: OpenSSH retries kbd-interactive up to
        # NumberOfPasswordPrompts (default 3) times per connection, each retry a
        # fresh Duo push. The `ssh -M` argv MUST force =1 so one connect fires at
        # most one push — otherwise the one-strike breaker under-counts pushes 3:1
        # and a few connects still reach the 10-strike Alliance lockout (#0317299).
        svc = SSHService()
        cfg = resolve_host("fir")
        spawned: list[tuple] = []

        async def _fake_call(service, verb, args=None):
            return {"status": "ok"}

        class _Proc:
            async def wait(self):
                return 0

        async def _spawn(*a, **k):
            spawned.append(a)
            return _Proc()

        monkeypatch.setattr(ssh_service.gatewayclient, "call", _fake_call)
        monkeypatch.setattr(ssh_service.asyncio, "create_subprocess_exec", _spawn)
        monkeypatch.setattr(svc, "_check_master", _true)

        await svc._attempt_master(cfg, svc._deviation_marker(cfg))

        assert len(spawned) == 1
        argv = list(spawned[0])
        # `-o NumberOfPasswordPrompts=1` must be present as an adjacent -o pair.
        pairs = [f"{argv[i+1]}" for i, tok in enumerate(argv[:-1]) if tok == "-o"]
        assert "NumberOfPasswordPrompts=1" in pairs, argv

    async def test_late_master_is_adopted_not_tripped(
            self, isolated_dirs, monkeypatch) -> None:
        # If the ControlMaster appears just after the poll window gives up
        # (_AttemptFailed), _do_connect must RE-CHECK and adopt it as connected —
        # not trip the breaker on an auth that actually succeeded late (which would
        # waste a live connection and spuriously page the operator).
        svc = SSHService()
        cfg = resolve_host("fir")
        checks = {"n": 0}

        async def _fail_to_bring_up(c, marker):
            raise ssh_service._AttemptFailed("ControlMaster did not appear")

        async def _master_late(_host):
            checks["n"] += 1
            return checks["n"] > 1   # False on probe_start, True on the adopt re-check

        monkeypatch.setattr(svc, "_check_master", _master_late)
        monkeypatch.setattr(svc, "_attempt_master", _fail_to_bring_up)
        result = await svc.connect("fir")
        assert result["status"] == "connected"       # adopted, not errored
        assert svc._read_lock(cfg) is None            # breaker NOT tripped
        assert svc._host("fir").state == ConnState.CONNECTED

    async def test_connect_waits_for_boot_reconcile(
            self, isolated_dirs, monkeypatch) -> None:
        # A connect must not start an ssh attempt until boot reconciliation (which
        # may still be reaping sockets) has finished.
        svc = SSHService()
        order: list[str] = []

        async def _slow_reconcile():
            await asyncio.sleep(0.02)
            order.append("reconcile")

        svc._reconcile_task = asyncio.create_task(_slow_reconcile())

        async def _attempt(c):
            order.append("connect")
            return SSHService._status_dict(c, "connected")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_do_connect", _attempt)
        await svc.connect("fir")
        assert order == ["reconcile", "connect"]

    async def test_disconnect_on_disconnected_is_noop(self, isolated_dirs) -> None:
        svc = SSHService()
        res = await svc.disconnect("fir")            # already DISCONNECTED
        assert res["status"] == "disconnected"
        assert svc._host("fir").state == ConnState.DISCONNECTED

    async def test_disconnect_connected_disposes(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        svc._host("fir").state = ConnState.CONNECTED
        monkeypatch.setattr(svc, "_exit_master", _true)   # teardown confirms gone
        res = await svc.disconnect("fir")
        assert res["status"] == "disconnected"
        assert "warning" not in res
        assert svc._host("fir").state == ConnState.DISCONNECTED

    async def test_disconnect_warns_when_teardown_unconfirmed(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        svc._host("fir").state = ConnState.CONNECTED
        monkeypatch.setattr(svc, "_exit_master", _false)  # master may still run
        res = await svc.disconnect("fir")
        assert res["status"] == "disconnected"
        assert "warning" in res                            # surfaced, not silent
        assert svc._host("fir").state == ConnState.DISCONNECTED

    async def test_do_connect_raise_does_not_strand_authenticating(
            self, isolated_dirs, monkeypatch) -> None:
        # An unexpected raise from _do_connect (e.g. OSError writing the lockfile)
        # must not leave the host wedged in AUTHENTICATING forever — it must reach a
        # terminal state and best-effort hold the host.
        svc = SSHService()
        cfg = resolve_host("fir")

        async def _boom(_c):
            raise RuntimeError("disk full writing lock")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_do_connect", _boom)
        monkeypatch.setattr(svc, "_alert", _noop_alert)

        res = await svc.connect("fir")
        assert res["status"] == "error"
        assert svc._host("fir").state == ConnState.DISCONNECTED   # NOT authenticating
        assert svc._read_lock(cfg) is not None                    # breaker held it


class TestStatusVerb:
    async def test_status_reports_each_state(self, isolated_dirs) -> None:
        svc = SSHService()
        svc._host("fir").state = ConnState.CONNECTED
        svc._host("sockeye").state = ConnState.AUTHENTICATING
        svc._host("chamois").state = ConnState.DISPOSING
        svc._write_lock(resolve_host("micb0"), "held")      # breaker lock → unavailable

        conns = (await svc.status())["connections"]
        assert conns["fir"]["status"] == "connected"
        assert conns["sockeye"]["status"] == "connecting"
        assert conns["chamois"]["status"] == "disconnecting"
        assert conns["micb0"]["status"] == "unavailable"
        assert conns["sockeye1"]["status"] == "disconnected"   # plain, no lock

    async def test_status_is_in_memory_not_probed(
            self, isolated_dirs, monkeypatch) -> None:
        # status() is a cached snapshot — it must not run any ssh probe, so a
        # CONNECTED host reads connected even if its master is actually dead.
        svc = SSHService()
        svc._host("fir").state = ConnState.CONNECTED

        async def _boom(_host):
            raise AssertionError("status must not probe the network")

        monkeypatch.setattr(svc, "_check_master", _boom)
        conns = (await svc.status())["connections"]
        assert conns["fir"]["status"] == "connected"


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

    async def test_reconcile_clears_stale_lock_on_adoption(
            self, isolated_dirs, monkeypatch) -> None:
        # Adopting a live master supersedes a leftover breaker lock — else, once
        # that master later dies and the host demotes, the stale lock would refuse
        # reconnection forever with no real fault behind it.
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "stale from a prior crash")

        async def _only_fir_up(host):
            return host == "fir"

        monkeypatch.setattr(svc, "_check_master", _only_fir_up)
        await svc._reconcile_on_boot()
        assert svc._host("fir").state == ConnState.CONNECTED
        assert svc._read_lock(cfg) is None          # stale lock cleared on adoption

    async def test_reap_double_checks_before_removing_socket(
            self, isolated_dirs, monkeypatch) -> None:
        # A transient false-negative (a live master briefly not answering) must not
        # get its socket reaped — reap only removes a socket that fails twice.
        live, _locks = isolated_dirs
        svc = SSHService()
        sockpath = live / "fir.computecanada.ca_22_phyberos"
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(sockpath))
        calls = {"n": 0}

        async def _flaky(_path):
            calls["n"] += 1
            return calls["n"] > 1    # first probe fails, second says alive

        monkeypatch.setattr(svc, "_check_socket", _flaky)
        try:
            await svc._reap_orphans()
        finally:
            s.close()

        assert sockpath.exists()     # survived the transient false-negative
        assert calls["n"] == 2       # it re-probed rather than reaping on one miss


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
