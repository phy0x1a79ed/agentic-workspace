from __future__ import annotations

import asyncio
import importlib
import json
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


async def _coro(value):
    """Wrap a plain value so it can stand in for an async method's return."""
    return value


async def _false(_host):
    return False


async def _true(_host):
    return True


async def _noop_alert(_text):
    return None


def _spawned() -> "ssh_service._AttemptRecord":
    """An attempt record for a connect that DID reach the ssh exec.

    The classification tests are all about what ssh's own output means, which
    presupposes ssh ran. A default record means the opposite — that nothing was
    spawned — and short-circuits the predicate to True.
    """
    return ssh_service._AttemptRecord(spawned=True)


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


@pytest.fixture(autouse=True)
def _no_live_gateway(monkeypatch):
    """No test may reach the live fleet.

    The service now asks the 2fa service to attribute a failure and to verify
    the approver's health. Left unpatched, those calls hit whatever gateway is
    running on the developer's machine, and the suite's verdict starts
    depending on whether Duo happens to be reachable right now. Tests that
    care about those answers patch this themselves — a later setattr wins.
    """
    async def _refuse(*_a, **_kw):
        raise RuntimeError("test tried to reach the live gateway")

    monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _refuse)
    monkeypatch.setattr(ssh_service.gatewayclient, "call", _refuse)


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

    async def test_preauth_failure_does_not_trip_breaker(
            self, isolated_dirs, monkeypatch) -> None:
        """A connect that dies before the auth phase spends no MFA attempt, so
        holding the host would be a false positive costing the operator an
        /approve. Seen live: both fir login nodes at `Exceeded MaxStartups`."""
        live, _ = isolated_dirs
        svc = SSHService()
        cfg = resolve_host("fir")

        tripped: list[str] = []

        async def _trip(c, r):
            tripped.append(r)

        async def _fail_attempt(c, marker, rec):
            # Write the capture where a real attempt writes it: after the spawn.
            # _do_connect_attempt truncates it on entry, so stderr seeded before
            # the call would describe some earlier attempt and is discarded.
            rec.spawned = True
            (live / "fir.connect.stderr").write_text(
                "kex_exchange_identification: Connection closed by remote host\n"
                "Connection closed by 206.12.125.2 port 22\n")
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(svc, "_trip_breaker", _trip)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _fail_attempt)

        result = await svc._do_connect_attempt(cfg, gated=False)

        assert result["status"] == "error"
        assert tripped == [], "pre-auth failure must not trip the breaker"
        assert svc._read_lock(cfg) is None, "no hold for a failure that spent nothing"
        assert "safe to retry" in result["error"]

    async def test_auth_failure_still_trips_breaker(
            self, isolated_dirs, monkeypatch) -> None:
        """The protective default is unchanged for anything that reached auth."""
        live, _ = isolated_dirs
        svc = SSHService()
        cfg = resolve_host("fir")

        tripped: list[str] = []

        async def _trip(c, r):
            tripped.append(r)

        async def _fail_attempt(c, marker, rec):
            rec.spawned = True   # ssh ran and failed; only that can hold
            (live / "fir.connect.stderr").write_text(
                "phyberos@fir.computecanada.ca: "
                "Permission denied (keyboard-interactive).\n")
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(svc, "_trip_breaker", _trip)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _fail_attempt)

        await svc._do_connect_attempt(cfg, gated=False)
        assert len(tripped) == 1, "an auth failure must still hold the host"

    async def test_no_unlock_verb_or_method(self) -> None:
        # Agents must not be able to clear their own hold.
        from awm.ssh import hub_adapter
        assert "unlock" not in hub_adapter.HANDLERS
        verbs = [f["name"] for f in hub_adapter.API_MANIFEST["functions"]]
        assert "unlock" not in verbs
        assert not hasattr(SSHService, "unlock")
        # Pinned, not merely filtered: adding ANY verb to this service has to
        # come here first and be argued for. A hold is lifted only by an
        # operator /approve, never by something an agent can call.
        assert set(hub_adapter.HANDLERS) == {
            "connect", "disconnect", "status", "notify_test", "receive_test",
        }
        assert set(verbs) == set(hub_adapter.HANDLERS)

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

    async def test_notify_test_surfaces_result_and_writes_no_lockfile(
            self, isolated_dirs, monkeypatch) -> None:
        # The self-test fires the real social send, RETURNS its result (does not
        # swallow), carries a clearly-test message, and mutates no breaker state.
        _, locks = isolated_dirs
        svc = SSHService()
        captured: list[str] = []

        async def _capture_send(text):
            captured.append(text)
            return {"ok": True, "id": "msg-123"}

        monkeypatch.setattr(svc, "_send_social", _capture_send)
        out = await svc.notify_test("[selftest]")

        assert out["sent"] is True
        assert out["result"] == {"ok": True, "id": "msg-123"}
        assert out["channel"] == ssh_service._ALERT_CHANNEL
        assert len(captured) == 1
        # Unmistakably a test, and no /approve reflex bait for a fake device.
        assert "self-test" in captured[0].lower()
        assert "/approve" not in captured[0]
        assert "not a real lock" in captured[0].lower()
        # No lockfile / breaker state written.
        assert os.listdir(locks) == []

    async def test_notify_test_raises_on_failure(
            self, isolated_dirs, monkeypatch) -> None:
        # Unlike _alert, the self-test must FAIL LOUDLY — a broken notify wire is
        # exactly what it exists to surface.
        svc = SSHService()

        async def _explode(_text):
            raise RuntimeError("social down")

        monkeypatch.setattr(svc, "_send_social", _explode)
        with pytest.raises(RuntimeError, match="social down"):
            await svc.notify_test()

    async def test_notify_test_verb_registered(self) -> None:
        from awm.ssh import hub_adapter
        assert "notify_test" in hub_adapter.HANDLERS
        verbs = [f["name"] for f in hub_adapter.API_MANIFEST["functions"]]
        assert "notify_test" in verbs

    def test_failure_reason_includes_askpass_deviation(
            self, isolated_dirs) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        marker = svc._deviation_marker(cfg)
        with open(marker, "w") as f:
            f.write("unrecognized prompt\n")
        reason = svc._failure_reason(cfg, marker, "base failure")
        assert "askpass deviation" in reason


class TestPreAuthClassification:
    """`_is_preauth_failure` decides whether a failed connect spent an MFA attempt.

    The two mistakes are NOT symmetric: calling an auth failure "pre-auth" means we
    keep retrying and burn the budget toward a real provider lockout, while calling
    a pre-auth failure "auth" merely costs a spurious /approve. Every test here
    pins the conservative direction.
    """

    def _svc_cfg(self, live, stderr: str | None):
        svc = SSHService()
        cfg = resolve_host("fir")
        if stderr is not None:
            (live / "fir.connect.stderr").write_text(stderr)
        return svc, cfg

    @pytest.mark.parametrize("blob", [
        "kex_exchange_identification: Connection closed by remote host",
        "Exceeded MaxStartups",
        "ssh: connect to host fir port 22: Connection refused",
        "ssh: Could not resolve hostname fir: Name or service not known",
        "ssh: connect to host fir port 22: No route to host",
        "ssh: connect to host fir port 22: Network is unreachable",
        "Host key verification failed.",
        "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@",
    ])
    def test_unambiguous_preauth_markers(self, isolated_dirs, blob) -> None:
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, blob)
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is True

    @pytest.mark.parametrize("blob", [
        "Permission denied (keyboard-interactive).",
        "Too many authentication failures",
        "Authentication failed.",
        "Your account is locked",
        "Duo two-factor login for phyberos",
        "MFA required",
    ])
    def test_auth_phase_markers_are_not_preauth(self, isolated_dirs, blob) -> None:
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, blob)
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False

    def test_auth_marker_vetoes_a_preauth_marker(self, isolated_dirs) -> None:
        """THE safety-critical case: if both appear, auth wins and we hold. An ssh
        run that failed auth and then lost the connection must never read as
        'spent nothing, retry freely'."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(
            live,
            "Permission denied (keyboard-interactive).\n"
            "kex_exchange_identification: Connection closed by remote host\n")
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False

    def test_askpass_deviation_decides_when_ssh_named_no_phase(
            self, isolated_dirs) -> None:
        """With no phase marker in ssh's own stderr, the deviation marker is the
        only evidence and its conservative reading stands: hold."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, "something we do not recognise")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text("deviation")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is False

    def test_a_non_duo_deviation_does_not_veto(self, isolated_dirs) -> None:
        """The live failure this came from (altair's first fir connect, 2026-07-29).

        A brand-new node had no host key for fir. `ssh` invokes SSH_ASKPASS for a
        host-key confirmation too, so the askpass was handed a prompt that was not
        a Duo menu and refused, fail-closed — correctly. Two things then misread
        that refusal as "Duo was reached": the marker vetoed unconditionally, and
        the askpass's own message ("not a known **Duo** menu") matched the
        auth-phase scan of the shared stderr stream. Host-key verification precedes
        authentication in the protocol, so no push can have fired — yet the arbiter
        held `fir` fleet-wide for a failure that provably spent nothing.
        """
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(
            live,
            "awm-duo-askpass: REFUSING — unrecognized prompt (not a known Duo "
            "menu). Not answering (fail-closed).\nHost key verification failed.\n")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text("unrecognized prompt (not a known Duo menu)")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is True

    def test_a_duo_menu_deviation_still_vetoes(self, isolated_dirs) -> None:
        """The askpass's other refusal reasons all follow a real Duo menu, so they
        keep holding — narrowing one case must not loosen those."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, "Host key verification failed.")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text(
            "device 'alliance' is on Duo push-hold (auto-clears 900s)")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is False

    def test_a_mixed_deviation_record_vetoes(self, isolated_dirs) -> None:
        """The marker is appended to, so one run can record several reasons. A
        single Duo-menu reason among them keeps the veto."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, "Host key verification failed.")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text(
            "unrecognized prompt (not a known Duo menu)\n"
            "no Duo option matched our devices (awm|Mira)\n")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is False

    def test_the_askpass_own_message_is_not_read_as_ssh_output(
            self, isolated_dirs) -> None:
        """The askpass shares ssh's stderr and its refusal text says "Duo". Scanning
        the raw blob turned a refusal into proof the Duo phase was reached."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(
            live,
            "awm-duo-askpass: REFUSING — unrecognized prompt (not a known Duo "
            "menu). Not answering (fail-closed).\n"
            "kex_exchange_identification: Connection closed by remote host\n")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text("unrecognized prompt (not a known Duo menu)")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is True

    def test_an_auth_marker_still_vetoes_even_with_a_preauth_phase_and_marker(
            self, isolated_dirs) -> None:
        """Loosening the marker rule must not loosen the auth veto."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(
            live,
            "Duo two-factor login for phyberos\nHost key verification failed.\n")
        marker = svc._deviation_marker(cfg)
        Path(marker).write_text("deviation")
        assert svc._is_preauth_failure(cfg, marker, _spawned()) is False

    def test_missing_stderr_is_not_preauth(self, isolated_dirs) -> None:
        """No evidence is not evidence of safety — hold."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, None)
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False

    def test_empty_stderr_is_not_preauth(self, isolated_dirs) -> None:
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, "   \n\n")
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False

    def test_unrecognised_stderr_is_not_preauth(self, isolated_dirs) -> None:
        """Default is protective for anything we don't positively recognise."""
        live, _ = isolated_dirs
        svc, cfg = self._svc_cfg(live, "some novel ssh explosion nobody has seen")
        assert svc._is_preauth_failure(cfg, svc._deviation_marker(cfg), _spawned()) is False


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

        async def _hang(c, marker, rec):   # never brings up the master
            rec.spawned = True
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

        async def _fake_call(service, verb, args=None, **_kw):
            # **_kw absorbs the as_/timeout kwargs call_maybe_peer now forwards.
            calls.append((service, verb, args))
            # Pretend a burst is already live — the old code would skip arming.
            return {"status": "ok", "burst_active": True}

        class _Proc:
            pid = 424242

            async def wait(self):
                return 0

        async def _spawn(*a, **k):
            return _Proc()

        async def _fake_maybe_peer(_peer, service, verb, args=None, **kw):
            return await _fake_call(service, verb, args, **kw)

        monkeypatch.setattr(ssh_service.gatewayclient, "call", _fake_call)
        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer",
                            _fake_maybe_peer)
        monkeypatch.setattr(ssh_service.asyncio, "create_subprocess_exec", _spawn)
        monkeypatch.setattr(svc, "_check_master", _true)    # master appears at once

        await svc._attempt_master(cfg, svc._deviation_marker(cfg),
                                  ssh_service._AttemptRecord())

        burst_calls = [c for c in calls if c[1] == "burst"]
        assert len(burst_calls) == 1
        assert burst_calls[0][2]["count"] == 1
        assert burst_calls[0][2]["device"] == "alliance"
        # And no 'status' pre-check gate remains.
        assert not any(c[1] == "status" for c in calls)

    async def test_late_master_is_adopted_not_tripped(
            self, isolated_dirs, monkeypatch) -> None:
        # If the ControlMaster appears just after the poll window gives up
        # (_AttemptFailed), _do_connect must RE-CHECK and adopt it as connected —
        # not trip the breaker on an auth that actually succeeded late (which would
        # waste a live connection and spuriously page the operator).
        svc = SSHService()
        cfg = resolve_host("fir")
        checks = {"n": 0}

        async def _fail_to_bring_up(c, marker, rec):
            rec.spawned = True
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
    """Behavioural checks on the installed shell helpers (skip if absent).

    Every one of these points the askpass at a throwaway AWM_DUO_LOCK_DIR. It
    keeps its rate-cap state there, and answering counts toward a cap that trips
    a 30-minute push-hold refusing ALL Duo answers for the device. Run against
    the default dir, three runs of this suite inside ten minutes therefore break
    the real login path — which is exactly what happened on 2026-09-01.
    """

    @pytest.mark.skipif(not _GUARD.exists(), reason="guard not installed")
    def test_guard_exits_nonzero_and_is_loud(self) -> None:
        r = subprocess.run([str(_GUARD), "fir", "22"], capture_output=True,
                           text=True)
        assert r.returncode != 0
        assert "BLOCKED" in r.stderr

    @pytest.mark.skipif(not _ASKPASS.exists(), reason="askpass not installed")
    def test_askpass_answers_matching_device(self, tmp_path) -> None:
        prompt = ("Duo two-factor login\n\n 1. Duo Push to Mira\n"
                  " 2. Phone call\n\nPasscode or option (1-2): ")
        r = subprocess.run([str(_ASKPASS), prompt], capture_output=True,
                           text=True,
                           env={**os.environ,
                                "AWM_DUO_LOCK_DIR": str(tmp_path)})
        assert r.returncode == 0
        assert r.stdout.strip() == "1"

    @pytest.mark.skipif(not _ASKPASS.exists(), reason="askpass not installed")
    def test_askpass_refuses_unrecognized_prompt(self, tmp_path) -> None:
        marker = tmp_path / "mk"
        r = subprocess.run([str(_ASKPASS), "password: "],
                           capture_output=True, text=True,
                           env={**os.environ,
                                "AWM_SSH_ASKPASS_MARKER": str(marker),
                                "AWM_DUO_LOCK_DIR": str(tmp_path)})
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
                                "AWM_SSH_ASKPASS_MARKER": str(marker),
                                "AWM_DUO_LOCK_DIR": str(tmp_path)})
        assert r.returncode != 0
        assert r.stdout.strip() == ""
        assert marker.exists()


class _FakeBridge:
    """Stand-in for the direct-session bridge websocket a lease handler holds.

    ``client_frames`` are the frames the requester "sends" (e.g. a verdict); when
    they run out the async-for ends — modelling a socket close (a drop if no
    verdict was seen)."""

    def __init__(self, client_frames):
        self.sent = []                      # frames the handler sent to the client
        self._frames = list(client_frames)
        self.closed = False

    async def send(self, raw):
        self.sent.append(raw)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class _FakeCtx:
    def __init__(self, host, bridge, node=None):
        self.init = {"host": host}
        if node is not None:
            self.init["node"] = node
        self._bridge = bridge

    async def open_bridge(self):
        return self._bridge


class TestSlotArbiter:
    """The fleet-global single-attempt gate: the _slot_acquire/_slot_release DFA
    and the _lease_session direct-session handler."""

    async def test_acquire_grants_from_idle_then_busy(self, isolated_dirs) -> None:
        svc = SSHService()
        status, token = await svc._slot_acquire("fir")
        assert status == "granted" and token
        # LEASED: a second acquire for the same host is refused BUSY.
        status2, token2 = await svc._slot_acquire("fir")
        assert status2 == "busy" and token2 is None

    async def test_release_ok_frees_slot(self, isolated_dirs) -> None:
        svc = SSHService()
        _, token = await svc._slot_acquire("fir")
        await svc._slot_release("fir", token, ok=True)
        assert svc._read_lock(resolve_host("fir")) is None
        assert "fir" not in svc._leased
        status, _ = await svc._slot_acquire("fir")     # IDLE again → granted
        assert status == "granted"

    async def test_release_fail_locks_and_alerts(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        alerts: list[str] = []

        async def _capture(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture)
        _, token = await svc._slot_acquire("fir")
        await svc._slot_release("fir", token, ok=False, reason="master never came up")

        assert svc._read_lock(resolve_host("fir")) is not None   # LOCKED persisted
        assert len(alerts) == 1 and "fir" in alerts[0]           # arbiter is notifier
        assert "/approve" in alerts[0]
        assert "master never came up" in alerts[0]
        # LOCKED: a later acquire is DENIED (no approval window).
        status, _ = await svc._slot_acquire("fir")
        assert status == "locked"

    async def test_release_idempotent_by_lease_id(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        alerts: list[str] = []

        async def _capture(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture)
        _, token = await svc._slot_acquire("fir")
        await svc._slot_release("fir", token, ok=False, reason="boom")
        # A stale/duplicate release with the now-consumed token must be a no-op:
        # it can neither clear the lock nor double-page.
        await svc._slot_release("fir", token, ok=True)
        assert svc._read_lock(resolve_host("fir")) is not None
        assert len(alerts) == 1

    async def test_approve_window_clears_and_grants_one_shot(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()

        async def _capture(text):
            pass

        monkeypatch.setattr(svc, "_alert", _capture)
        cfg = resolve_host("fir")
        _, token = await svc._slot_acquire("fir")
        await svc._slot_release("fir", token, ok=False, reason="boom")
        assert svc._read_lock(cfg) is not None

        # Operator /approve opens the window → next acquire clears + grants.
        svc._handle_approve({"command": "approve", "device": cfg.twofa_device})
        status, token2 = await svc._slot_acquire("fir")
        assert status == "granted" and token2
        assert svc._read_lock(cfg) is None

        # One-shot consumed: a fresh failure re-locks and is NOT auto-cleared.
        await svc._slot_release("fir", token2, ok=False, reason="boom2")
        status3, _ = await svc._slot_acquire("fir")
        assert status3 == "locked"

    async def test_lease_session_grant_then_verdict_ok(self, isolated_dirs) -> None:
        svc = SSHService()
        bridge = _FakeBridge(['{"verdict":"ok","reason":""}'])
        await svc._lease_session(_FakeCtx("fir", bridge))
        assert json.loads(bridge.sent[0]) == {"lease": "granted"}
        assert bridge.closed
        assert svc._read_lock(resolve_host("fir")) is None       # freed
        assert "fir" not in svc._leased

    async def test_lease_session_verdict_fail_locks(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        alerts: list[str] = []

        async def _capture(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture)
        bridge = _FakeBridge(['{"verdict":"fail","reason":"master never came up"}'])
        await svc._lease_session(_FakeCtx("fir", bridge))
        assert json.loads(bridge.sent[0]) == {"lease": "granted"}
        assert svc._read_lock(resolve_host("fir")) is not None
        assert len(alerts) == 1 and "master never came up" in alerts[0]

    async def test_lease_session_drop_without_verdict_locks(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        alerts: list[str] = []

        async def _capture(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture)
        bridge = _FakeBridge([])                       # client drops, no verdict
        await svc._lease_session(_FakeCtx("fir", bridge))
        assert json.loads(bridge.sent[0]) == {"lease": "granted"}
        assert svc._read_lock(resolve_host("fir")) is not None    # drop → LOCKED
        assert len(alerts) == 1

    async def test_lease_session_busy_when_already_leased(
            self, isolated_dirs) -> None:
        svc = SSHService()
        await svc._slot_acquire("fir")                 # pre-hold the slot
        bridge = _FakeBridge([])
        await svc._lease_session(_FakeCtx("fir", bridge))
        assert json.loads(bridge.sent[0])["lease"] == "busy"
        assert bridge.closed

    async def test_lease_session_unknown_host(self, isolated_dirs) -> None:
        svc = SSHService()
        bridge = _FakeBridge([])
        await svc._lease_session(_FakeCtx("nope", bridge))
        assert json.loads(bridge.sent[0])["lease"] == "error"

    async def test_gated_attempt_does_not_trip_local_breaker(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        tripped: list[str] = []

        async def _trip(c, r):
            tripped.append(r)

        async def _fail_attempt(c, marker, rec):
            rec.spawned = True   # ssh ran and failed; only that can hold
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(svc, "_trip_breaker", _trip)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _fail_attempt)
        result = await svc._do_connect_attempt(cfg, gated=True)

        assert result["status"] == "error"
        assert result.get("_lock_reason")             # reason surfaced for the verdict
        assert tripped == []                          # arbiter owns LOCKED, not this
        assert svc._read_lock(cfg) is None            # gated writes no local lock

    async def test_gated_preauth_failure_frees_slot_without_locking(
            self, isolated_dirs, monkeypatch) -> None:
        """The lease verdict answers "was the MFA budget spent?", not "did the
        connect succeed". A pre-auth failure spent nothing, so the arbiter must
        free the slot and leave the host UNLOCKED — otherwise a saturated login
        node (MaxStartups) holds fir fleet-wide behind an /approve."""
        live, _ = isolated_dirs
        monkeypatch.delenv("AWM_SSH_SLOT_PEER", raising=False)  # local arbiter
        svc = SSHService()
        cfg = resolve_host("fir")

        alerts: list[str] = []
        monkeypatch.setattr(svc, "_alert", lambda t: alerts.append(t) or _noop_alert(t))

        async def _fail_attempt(c, marker, rec):
            rec.spawned = True   # ssh ran and failed; only that can hold
            (live / "fir.connect.stderr").write_text(
                "kex_exchange_identification: Connection closed by remote host")
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _fail_attempt)

        result = await svc._connect_via_local_arbiter(cfg)

        assert result["status"] == "error"
        assert svc._read_lock(cfg) is None, "pre-auth must not LOCK the arbiter"
        assert alerts == [], "nothing to page the operator about — nothing was spent"
        assert "fir" not in svc._leased, "slot must be freed for the next attempt"
        assert "_preauth" not in result, "internal flag must not leak to the agent"
        assert "_lock_reason" not in result

    async def test_gated_auth_failure_still_locks_the_arbiter(
            self, isolated_dirs, monkeypatch) -> None:
        """The fleet-global hold is unchanged for a failure that reached auth."""
        live, _ = isolated_dirs
        monkeypatch.delenv("AWM_SSH_SLOT_PEER", raising=False)
        svc = SSHService()
        cfg = resolve_host("fir")
        (live / "fir.connect.stderr").write_text(
            "Permission denied (keyboard-interactive).")

        monkeypatch.setattr(svc, "_alert", _noop_alert)

        async def _fail_attempt(c, marker, rec):
            rec.spawned = True   # ssh ran and failed; only that can hold
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _fail_attempt)

        await svc._connect_via_local_arbiter(cfg)
        assert svc._read_lock(cfg) is not None, "auth failure must still hold fir"
        assert "fir" not in svc._leased

    async def test_verdict_ok_semantics(self) -> None:
        assert SSHService._verdict_ok({"status": "connected"}) is True
        assert SSHService._verdict_ok({"status": "error", "_preauth": True}) is True
        assert SSHService._verdict_ok({"status": "error", "_preauth": False}) is False
        assert SSHService._verdict_ok({"status": "error"}) is False

    async def test_do_connect_routes_2fa_host_through_gated_arbiter(
            self, isolated_dirs, monkeypatch) -> None:
        monkeypatch.delenv("AWM_SSH_SLOT_PEER", raising=False)  # local arbiter
        svc = SSHService()
        seen = {}

        async def _fake_attempt(cfg, gated=False):
            seen["gated"] = gated
            return svc._status_dict(cfg, "connected")

        monkeypatch.setattr(svc, "_do_connect_attempt", _fake_attempt)
        result = await svc._do_connect(resolve_host("fir"))
        assert result["status"] == "connected"
        assert seen["gated"] is True                  # gated arbiter path
        assert "fir" not in svc._leased               # released after success

    async def test_do_connect_non_2fa_host_stays_local(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        seen = {}

        async def _fake_attempt(cfg, gated=False):
            seen["gated"] = gated
            return svc._status_dict(cfg, "connected")

        monkeypatch.setattr(svc, "_do_connect_attempt", _fake_attempt)
        result = await svc._do_connect(resolve_host("chamois"))  # no twofa_device
        assert result["status"] == "connected"
        assert seen["gated"] is False                 # ungated per-node local path


class TestApproveObservability:
    """The /approve wire must be visible: an acknowledgement the operator can
    see, and a health block they can check before an emergency."""

    async def test_ack_failure_cannot_close_the_window(
            self, isolated_dirs, monkeypatch) -> None:
        """The window is decided synchronously; the receipt is a separate,
        best-effort step afterwards. A Discord outage must not be able to
        swallow a recovery the operator already authorised."""
        svc = SSHService()
        cfg = resolve_host("fir")

        async def _boom(*_a, **_kw):
            raise RuntimeError("discord is down")

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _boom)
        await svc._on_approve_event({
            "command": "approve", "device": cfg.twofa_device,
            "account": "discord-bot", "channel_id": "123",
        })
        assert svc._approve_active(cfg.twofa_device)

    async def test_ack_is_sent_for_a_valid_approve(
            self, isolated_dirs, monkeypatch) -> None:
        sent = []

        async def _capture(peer, service, fn, args):
            sent.append((service, fn, args))
            return {"ok": True}

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _capture)
        svc = SSHService()
        cfg = resolve_host("fir")
        await svc._on_approve_event({
            "command": "approve", "device": cfg.twofa_device,
            "account": "discord-bot", "channel_id": "123",
        })
        assert len(sent) == 1
        service, fn, args = sent[0]
        assert (service, fn) == ("social", "send")
        assert cfg.twofa_device in args["text"]
        # A receipt, never an instruction: nothing here should train a reflex
        # to re-run /approve, which spends multi-factor budget.
        assert "/approve" not in args["text"]

    async def test_unrelated_commands_are_not_acked(
            self, isolated_dirs, monkeypatch) -> None:
        sent = []

        async def _capture(*a, **kw):
            sent.append(a)
            return {"ok": True}

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _capture)
        svc = SSHService()
        await svc._on_approve_event({"command": "something-else"})
        assert sent == []

    async def test_status_reports_a_missing_subscription_as_unhealthy(
            self, isolated_dirs) -> None:
        svc = SSHService()          # listener never started
        st = await svc.status()
        assert st["subscription"]["healthy"] is False
        assert st["approval_windows"] == {}

    async def test_status_reports_open_windows_read_only(
            self, isolated_dirs) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._handle_approve({"command": "approve", "device": cfg.twofa_device})
        st = await svc.status()
        assert st["approval_windows"][cfg.twofa_device] > 0


class TestReceiveTest:
    """The inbound self-test: prove this service can still HEAR an /approve."""

    async def test_probe_is_intercepted_and_never_reaches_the_handler(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()

        emitted = {}

        async def _emit(peer, service, fn, args):
            emitted.update(args)
            # Deliver it the way the subscription would.
            svc._intercept_probe({"command": ssh_service._PROBE_COMMAND,
                                  "nonce": args["nonce"]})
            return {"ok": True}

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _emit)
        svc._approve_sub = object()   # a listener exists
        result = await svc.receive_test(timeout=2.0)
        assert result["received"] is True
        # Inert: no window opened, no lockfile written.
        assert svc._approve_until == {}
        assert not os.path.exists(lock_path(resolve_host("fir")))

    async def test_deafness_fails_loudly(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()

        async def _emit_into_the_void(*_a, **_kw):
            return {"ok": True}

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer",
                            _emit_into_the_void)
        svc._approve_sub = object()
        with pytest.raises(RuntimeError, match="DEAF"):
            await svc.receive_test(timeout=0.2)

    async def test_emit_failure_does_not_read_as_deafness(
            self, isolated_dirs, monkeypatch) -> None:
        """An un-upgraded peer that lacks emit_probe is a different fault, and
        must not be reported as 'you cannot receive'."""
        svc = SSHService()

        async def _no_such_verb(*_a, **_kw):
            raise RuntimeError("unknown function emit_probe")

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer",
                            _no_such_verb)
        svc._approve_sub = object()
        with pytest.raises(RuntimeError) as exc:
            await svc.receive_test(timeout=0.2)
        assert "could not ask social" in str(exc.value)
        assert "DEAF" not in str(exc.value)


class TestActiveSignalUnlock:
    """A hold releases only on an affirmative assertion that it is safe to.
    Never because time passed, never because no error was seen."""

    def _fake_2fa(self, monkeypatch, *, ping=None, reachability=None,
                  boom: bool = False):
        calls = []

        async def _call(peer, service, fn, args=None, **kw):
            calls.append((service, fn, args))
            if boom:
                raise RuntimeError("2fa unreachable")
            if service == "social":
                return {"ok": True}
            if fn == "ping":
                return ping if ping is not None else {"reachable": False}
            if fn == "reachability":
                return reachability if reachability is not None else {}
            return {}

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _call)
        return calls

    async def test_timeout_while_the_approver_was_dead_is_self_attributed(
            self, isolated_dirs, monkeypatch) -> None:
        """The 2026-07-26 shape: fir's hold read 'connect exceeded 120s' — true
        but misattributed. Nothing was wrong with fir; our own approver could
        not reach Duo."""
        self._fake_2fa(monkeypatch, reachability={"last_reachable_at": None})
        svc = SSHService()
        cfg = resolve_host("fir")
        await svc._trip_breaker(cfg, "connect exceeded 120s")
        assert svc._read_lock(cfg)["cause"] == ssh_service._CAUSE_SELF

    async def test_a_live_probe_that_answers_makes_it_external(
            self, isolated_dirs, monkeypatch) -> None:
        """A working approver means the failure was not ours.

        Attribution asks the approver rather than reading how long it has been
        since anyone did. The old recency test passed for the wrong reason: a
        timestamp that only ``ping`` refreshed decayed with idle time, so a
        perfectly healthy but quiet approver was recorded as unavailable — and
        that cause licenses one automatic retry, i.e. one more Duo push at a
        host that had already failed.
        """
        calls = self._fake_2fa(monkeypatch, ping={"ok": True, "reachable": True})
        svc = SSHService()
        cfg = resolve_host("fir")
        await svc._trip_breaker(cfg, "connect exceeded 120s")
        assert svc._read_lock(cfg)["cause"] == ssh_service._CAUSE_EXTERNAL
        assert ("2fa", "ping", {"device": "alliance"}) in calls, \
            "attribution must PROBE the approver, not infer from a timestamp"

    async def test_an_auth_rejection_is_never_self_attributed(
            self, isolated_dirs, monkeypatch) -> None:
        """The veto. Auth was reached, so an MFA attempt was spent and the
        failure is not ours to forgive."""
        self._fake_2fa(monkeypatch, reachability={"last_reachable_at": None})
        svc = SSHService()
        cfg = resolve_host("fir")
        await svc._trip_breaker(
            cfg, "connect exceeded 120s; ssh: Permission denied "
                 "(keyboard-interactive).")
        assert svc._read_lock(cfg)["cause"] == ssh_service._CAUSE_EXTERNAL

    async def test_an_unaskable_2fa_service_is_never_self_attributed(
            self, isolated_dirs, monkeypatch) -> None:
        """Failing to establish self-attribution is not evidence of it."""
        self._fake_2fa(monkeypatch, boom=True)
        svc = SSHService()
        cfg = resolve_host("fir")
        monkeypatch.setattr(svc, "_alert", _noop_alert)
        await svc._trip_breaker(cfg, "connect exceeded 120s")
        assert svc._read_lock(cfg)["cause"] == ssh_service._CAUSE_EXTERNAL

    async def test_external_holds_are_never_self_cleared(
            self, isolated_dirs, monkeypatch) -> None:
        self._fake_2fa(monkeypatch, ping={"reachable": True})
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "Permission denied",
                        cause=ssh_service._CAUSE_EXTERNAL)
        assert await svc._maybe_self_clear(cfg) is False
        assert svc._read_lock(cfg) is not None

    async def test_a_self_inflicted_hold_needs_a_positive_assertion(
            self, isolated_dirs, monkeypatch) -> None:
        """Not 'no error seen' — a live round-trip, or the hold stands."""
        self._fake_2fa(monkeypatch, ping={"reachable": False})
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "connect exceeded 120s",
                        cause=ssh_service._CAUSE_SELF)
        assert await svc._maybe_self_clear(cfg) is False
        assert svc._read_lock(cfg) is not None

    async def test_a_healthy_approver_releases_a_self_inflicted_hold(
            self, isolated_dirs, monkeypatch) -> None:
        self._fake_2fa(monkeypatch, ping={"reachable": True})
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "connect exceeded 120s",
                        cause=ssh_service._CAUSE_SELF)
        assert await svc._maybe_self_clear(cfg) is True
        assert svc._read_lock(cfg) is None

    async def test_self_clear_grants_permission_not_an_attempt(
            self, isolated_dirs, monkeypatch) -> None:
        """The G1 boundary. Clearing a hold makes the ask possible; it must
        never BE the ask. Nothing here may reach ssh."""
        self._fake_2fa(monkeypatch, ping={"reachable": True})
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "connect exceeded 120s",
                        cause=ssh_service._CAUSE_SELF)

        async def _boom(*a, **k):
            raise AssertionError("no connect may be started by a self-clear")

        monkeypatch.setattr(svc, "_check_master", _boom)
        monkeypatch.setattr(svc, "_attempt_master", _boom)
        monkeypatch.setattr(svc, "_do_connect", _boom)

        assert await svc._maybe_self_clear(cfg) is True
        await asyncio.sleep(0.05)          # nothing scheduled, nothing fires

    async def test_an_unreadable_lockfile_is_never_self_cleared(
            self, isolated_dirs, monkeypatch) -> None:
        """A torn read degrades to 'locked for an unreadable reason', which
        carries no cause — and no cause means operator-only."""
        self._fake_2fa(monkeypatch, ping={"reachable": True})
        svc = SSHService()
        cfg = resolve_host("fir")
        Path(lock_path(cfg)).write_text("{not json")
        assert await svc._maybe_self_clear(cfg) is False

    async def test_lockfile_write_is_atomic(
            self, isolated_dirs) -> None:
        svc = SSHService()
        cfg = resolve_host("fir")
        svc._write_lock(cfg, "reason", cause=ssh_service._CAUSE_SELF)
        _, locks = isolated_dirs
        assert [p.name for p in locks.iterdir()] == ["fir.lock"]
        assert json.loads(Path(lock_path(cfg)).read_text())["cause"] == \
            ssh_service._CAUSE_SELF


class TestNodeIdentity:
    """Which node an alert says it came from.

    `socket.gethostname()` used to be the only node-identity in the tree, and
    mira's hostname is `pavilion` — so its lockout alerts, the ones that decide
    whether an operator reaches for `/approve`, named a machine nobody in the
    fleet calls mira. The name now comes from the node env.
    """

    def _reloaded(self, monkeypatch, *, declared: str | None, hostname: str):
        """Re-import the service with a given env, since _NODE_NAME is bound at
        import time (the gateway loads .awm/env before spawning any service)."""
        import importlib
        if declared is None:
            monkeypatch.delenv("AWM_NODE_NAME", raising=False)
        else:
            monkeypatch.setenv("AWM_NODE_NAME", declared)
        monkeypatch.setattr(socket, "gethostname", lambda: hostname)
        return importlib.reload(ssh_service)

    def test_the_declared_fleet_name_wins(self, monkeypatch) -> None:
        mod = self._reloaded(monkeypatch, declared="mira", hostname="pavilion")
        try:
            assert mod._NODE_NAME == "mira"
        finally:
            importlib.reload(mod)

    def test_the_hostname_is_the_fallback(self, monkeypatch) -> None:
        mod = self._reloaded(monkeypatch, declared=None, hostname="pavilion")
        try:
            assert mod._NODE_NAME == "pavilion"
        finally:
            importlib.reload(mod)

    def test_the_lockout_page_names_the_node_whose_attempt_failed(
            self, monkeypatch) -> None:
        """The message that actually pages the operator carried no node name at
        all before. What it must name is the *requester* — not whoever generated
        the text."""
        mod = self._reloaded(monkeypatch, declared="mira", hostname="pavilion")
        try:
            text = mod.SSHService()._lock_alert_text(
                resolve_host("fir"), "connect timed out", requester="capella")
            assert "capella" in text
            assert "fir" in text
            # The arbiter must not put its own name on someone else's failure.
            assert "mira" not in text
        finally:
            importlib.reload(mod)

    def test_an_unattributed_failure_says_so_rather_than_guessing(
            self, monkeypatch) -> None:
        """A lease dropped by a client too old to report its node. Better an
        honest "unidentified" than confidently naming the arbiter."""
        mod = self._reloaded(monkeypatch, declared="mira", hostname="pavilion")
        try:
            text = mod.SSHService()._lock_alert_text(
                resolve_host("fir"), "dropped", requester=None)
            assert "unidentified" in text
            assert "mira" not in text
        finally:
            importlib.reload(mod)

    async def test_the_local_breaker_path_names_itself(
            self, isolated_dirs, monkeypatch) -> None:
        """A non-2FA host keeps the per-node breaker, and there the requester
        genuinely IS this node — so the same message must name us."""
        mod = self._reloaded(monkeypatch, declared="altair", hostname="devbox-02")
        try:
            svc = mod.SSHService()
            alerts: list[str] = []

            async def _capture(text):
                alerts.append(text)

            monkeypatch.setattr(svc, "_alert", _capture)
            monkeypatch.setattr(svc, "_classify_cause",
                                lambda cfg, reason: _coro("other"))
            await svc._trip_breaker(resolve_host("fir"), "connect timed out")
            assert len(alerts) == 1
            assert "altair" in alerts[0]
            assert "unidentified" not in alerts[0]
        finally:
            importlib.reload(mod)

    def test_the_approve_ack_says_which_node_reopened(self, monkeypatch) -> None:
        """Two ssh services consume the same /approve; both acknowledge it."""
        mod = self._reloaded(monkeypatch, declared="mira", hostname="pavilion")
        try:
            text = mod.SSHService()._handle_approve({
                "command": "approve",
                "device": resolve_host("fir").twofa_device,
            })
            assert "mira" in text
            assert "pavilion" not in text
        finally:
            importlib.reload(mod)


class TestArbiterAttribution:
    """Who the arbiter says failed.

    The arbiter is the SOLE notifier on a LOCKED transition, and it usually is
    not the node that made the attempt: capella's `AWM_SSH_SLOT_PEER=mira` means
    a failed capella connect produces an alert generated on mira. So the
    requester's identity has to travel with the lease.
    """

    async def test_the_lease_init_frame_carries_the_requester(
            self, isolated_dirs, monkeypatch) -> None:
        """Covers the drop case, where there is no verdict frame to read."""
        svc = SSHService()
        alerts: list[str] = []

        async def _capture(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture)
        bridge = _FakeBridge([])                      # drops with no verdict
        await svc._lease_session(_FakeCtx("fir", bridge, node="capella"))
        assert len(alerts) == 1
        assert "capella" in alerts[0]

    async def test_the_verdict_frame_carries_the_requester(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        alerts: list[str] = []

        async def _capture(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture)
        bridge = _FakeBridge([json.dumps(
            {"verdict": "fail", "reason": "master never came up",
             "node": "altair"})])
        await svc._lease_session(_FakeCtx("fir", bridge))
        assert len(alerts) == 1
        assert "altair" in alerts[0]
        assert "master never came up" in alerts[0]

    async def test_a_client_that_reports_no_node_is_not_misattributed(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        alerts: list[str] = []

        async def _capture(text):
            alerts.append(text)

        monkeypatch.setattr(svc, "_alert", _capture)
        bridge = _FakeBridge([json.dumps({"verdict": "fail", "reason": "boom"})])
        await svc._lease_session(_FakeCtx("fir", bridge))
        assert len(alerts) == 1
        assert "unidentified" in alerts[0]

    async def test_the_requester_passes_its_own_name_when_acquiring(
            self, isolated_dirs, monkeypatch) -> None:
        """The other half of the wire: the connect path must actually send it."""
        seen: dict = {}

        class _Lease:
            granted = False
            status = "busy"
            reason = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        async def _acquire(peer, service, host, **kw):
            seen.update(peer=peer, service=service, host=host, **kw)
            return _Lease()

        monkeypatch.setattr(ssh_service.gatewayclient,
                            "acquire_lease_maybe_peer", _acquire)
        svc = SSHService()
        await svc._connect_via_arbiter_peer(resolve_host("fir"), "mira")
        assert seen["node"] == ssh_service._NODE_NAME


class _FakeProc:
    """An ssh that never finishes authenticating, and remembers being signalled."""

    def __init__(self, pid: int = 987654) -> None:
        self.pid = pid
        self.signals: list[int] = []

    async def wait(self):
        await asyncio.sleep(30.0)
        return 0


class TestChildReaping:
    """A connect that fails must leave no ssh behind.

    The process is spawned inside a coroutine the 120s wait_for cancels. Before
    this, the handle was a local in that frame and the frame simply went away —
    and the one cleanup on the path, `ssh -O exit`, speaks through a socket a
    pre-auth-hung ssh has never created. So it survived, holding an armed Duo
    window, until something killed the whole service.
    """

    def _patch_spawn(self, monkeypatch, proc):
        async def _spawn(*a, **k):
            assert k.get("start_new_session") is True, (
                "the child must lead its own process group, or reaping the "
                "group kills the service itself")
            return proc

        monkeypatch.setattr(ssh_service.asyncio, "create_subprocess_exec", _spawn)

    def _patch_killpg(self, monkeypatch, proc, *, alive_after_term=False):
        """Stand in for the group signal. Unless *alive_after_term*, the group is
        gone once SIGTERM lands, which is what the kernel reports as
        ProcessLookupError and what stops the escalation."""
        def _killpg(pid, sig):
            assert pid == proc.pid
            if proc.signals and not alive_after_term:
                raise ProcessLookupError(pid)
            proc.signals.append(sig)

        monkeypatch.setattr(ssh_service.os, "killpg", _killpg)

    async def test_timeout_reaps_the_spawned_ssh(
            self, isolated_dirs, monkeypatch) -> None:
        proc = _FakeProc()
        svc = SSHService()
        cfg = resolve_host("micb0")          # no 2FA device: no burst to fake
        self._patch_spawn(monkeypatch, proc)
        self._patch_killpg(monkeypatch, proc)
        monkeypatch.setattr(ssh_service, "_CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(ssh_service, "_REAP_GRACE_S", 0.01)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_exit_master", _true)
        monkeypatch.setattr(svc, "_trip_breaker", lambda c, r: _coro(None))
        monkeypatch.setattr(ssh_service.gatewayclient, "call",
                            lambda *a, **k: _coro({"status": "up"}))

        await svc._do_connect_attempt(cfg, gated=False)

        assert proc.signals == [ssh_service.signal.SIGTERM], \
            "a cancelled attempt must terminate the ssh it spawned, and stop " \
            "there once it is gone"

    async def test_a_process_that_ignores_sigterm_is_killed(
            self, isolated_dirs, monkeypatch) -> None:
        proc = _FakeProc()
        svc = SSHService()
        cfg = resolve_host("micb0")
        self._patch_spawn(monkeypatch, proc)
        self._patch_killpg(monkeypatch, proc, alive_after_term=True)
        monkeypatch.setattr(ssh_service, "_CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(ssh_service, "_REAP_GRACE_S", 0.01)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_exit_master", _true)
        monkeypatch.setattr(svc, "_trip_breaker", lambda c, r: _coro(None))
        monkeypatch.setattr(ssh_service.gatewayclient, "call",
                            lambda *a, **k: _coro({"status": "up"}))

        await svc._do_connect_attempt(cfg, gated=False)

        assert proc.signals == [ssh_service.signal.SIGTERM,
                                ssh_service.signal.SIGKILL]

    async def test_the_pid_file_is_removed_when_the_attempt_resolves(
            self, isolated_dirs, monkeypatch) -> None:
        live, _ = isolated_dirs
        svc = SSHService()
        cfg = resolve_host("micb0")
        proc = _FakeProc()
        self._patch_spawn(monkeypatch, proc)
        self._patch_killpg(monkeypatch, proc)
        monkeypatch.setattr(ssh_service, "_CONNECT_TIMEOUT", 0.05)
        monkeypatch.setattr(ssh_service, "_REAP_GRACE_S", 0.01)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_exit_master", _true)
        monkeypatch.setattr(svc, "_trip_breaker", lambda c, r: _coro(None))
        monkeypatch.setattr(ssh_service.gatewayclient, "call",
                            lambda *a, **k: _coro({"status": "up"}))

        await svc._do_connect_attempt(cfg, gated=False)

        assert not (live / "micb0.master.pid").exists()

    async def test_boot_reaps_a_master_stranded_by_a_previous_life(
            self, isolated_dirs, monkeypatch) -> None:
        """The in-process reap cannot cover a service that was itself killed."""
        live, _ = isolated_dirs
        (live / "fir.master.pid").write_text("31337\n")
        svc = SSHService()
        reaped: list[int] = []

        monkeypatch.setattr(svc, "_is_our_master", lambda pid: pid == 31337)
        monkeypatch.setattr(svc, "_reap_group",
                            lambda pid, host: _coro(reaped.append(pid)))

        await svc._reap_stranded_masters()

        assert reaped == [31337]
        assert not (live / "fir.master.pid").exists(), \
            "the record must not outlive the process it names"

    async def test_boot_leaves_a_pid_that_is_not_ours_alone(
            self, isolated_dirs, monkeypatch) -> None:
        """A pid is the least durable key there is — reuse must not kill a stranger."""
        live, _ = isolated_dirs
        (live / "fir.master.pid").write_text("31337\n")
        svc = SSHService()
        reaped: list[int] = []

        monkeypatch.setattr(svc, "_is_our_master", lambda pid: False)
        monkeypatch.setattr(svc, "_reap_group",
                            lambda pid, host: _coro(reaped.append(pid)))

        await svc._reap_stranded_masters()

        assert reaped == []
        assert not (live / "fir.master.pid").exists()

    def test_identity_comes_from_the_environment_not_the_command_line(self) -> None:
        """Our own process is not one of our masters, however it is spelled."""
        assert SSHService._is_our_master(os.getpid()) is False


class TestNothingWasSpent:
    """The breaker must hold a host only when an MFA attempt could be spent."""

    async def test_a_failure_before_the_spawn_is_never_a_hold(
            self, isolated_dirs, monkeypatch) -> None:
        """A dependency that fails before ssh runs cannot have fired a push.

        Seen live 2026-09-01: `vpn/up` answered HTTP 504, ssh never ran, and the
        arbiter held sockeye fleet-wide and paged the operator for it.
        """
        svc = SSHService()
        cfg = resolve_host("sockeye")
        tripped: list[str] = []

        async def _vpn_times_out(c, marker, rec):
            raise RuntimeError("gateway call vpn/up failed: HTTP 504")

        monkeypatch.setattr(svc, "_attempt_master", _vpn_times_out)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_trip_breaker",
                            lambda c, r: _coro(tripped.append(r)))

        result = await svc._do_connect_attempt(cfg, gated=False)

        assert result["status"] == "error"
        assert tripped == [], "nothing ran, so nothing can have been spent"
        assert svc._read_lock(cfg) is None
        assert "safe to retry" in result["error"]

    async def test_a_pre_spawn_failure_ignores_an_older_attempts_stderr(
            self, isolated_dirs, monkeypatch) -> None:
        """The capture describes whichever attempt last got as far as spawning.

        sockeye's lock on 2026-09-01 quoted "Success. Logging you in..." from a
        connect six days earlier, because the file was only ever truncated at the
        spawn.
        """
        live, _ = isolated_dirs
        (live / "sockeye.connect.stderr").write_text(
            "Permission denied (keyboard-interactive).\n")
        svc = SSHService()
        cfg = resolve_host("sockeye")

        async def _vpn_times_out(c, marker, rec):
            raise RuntimeError("gateway call vpn/up failed: HTTP 504")

        monkeypatch.setattr(svc, "_attempt_master", _vpn_times_out)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_trip_breaker", lambda c, r: _coro(None))

        await svc._do_connect_attempt(cfg, gated=False)

        assert (live / "sockeye.connect.stderr").read_text() == "", \
            "the capture must be emptied when the attempt starts"

    def _fake_2fa_status(self, monkeypatch, seen):
        async def _call(_peer, service, fn, args=None, **kw):
            if service == "2fa" and fn == "status":
                return {} if seen is None else {"transactions_seen": seen}
            return {}

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _call)

    async def _saw_nothing(self, monkeypatch, *, armed, now):
        svc = SSHService()
        cfg = resolve_host("fir")
        self._fake_2fa_status(monkeypatch, now)
        rec = ssh_service._AttemptRecord(spawned=True, twofa_seen_at_arm=armed)
        return await svc._duo_saw_nothing(cfg, rec)

    async def test_an_unmoved_duo_count_proves_nothing_was_spent(
            self, isolated_dirs, monkeypatch) -> None:
        """fir was under vendor maintenance and Duo saw nothing all window."""
        assert await self._saw_nothing(monkeypatch, armed=7, now=7) is True

    async def test_a_duo_transaction_still_holds(
            self, isolated_dirs, monkeypatch) -> None:
        assert await self._saw_nothing(monkeypatch, armed=7, now=8) is False

    async def test_an_unaskable_count_is_not_zero(
            self, isolated_dirs, monkeypatch) -> None:
        """No evidence is not evidence of none — the protective default stands."""
        assert await self._saw_nothing(monkeypatch, armed=None, now=7) is False
        assert await self._saw_nothing(monkeypatch, armed=7, now=None) is False

    async def test_a_count_that_went_backwards_is_not_zero(
            self, isolated_dirs, monkeypatch) -> None:
        """A lower reading means the approver restarted, not that nothing ran."""
        assert await self._saw_nothing(monkeypatch, armed=7, now=2) is False

    async def test_an_unreachable_approver_is_not_zero(
            self, isolated_dirs, monkeypatch) -> None:
        async def _boom(*a, **k):
            raise RuntimeError("2fa unreachable")

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _boom)
        svc = SSHService()
        rec = ssh_service._AttemptRecord(spawned=True, twofa_seen_at_arm=7)
        assert await svc._duo_saw_nothing(resolve_host("fir"), rec) is False

    async def test_a_host_with_no_device_has_no_witness(
            self, isolated_dirs, monkeypatch) -> None:
        svc = SSHService()
        rec = ssh_service._AttemptRecord(spawned=True, twofa_seen_at_arm=7)
        assert await svc._duo_saw_nothing(resolve_host("micb0"), rec) is False

    async def test_the_gated_path_frees_the_slot_when_duo_saw_nothing(
            self, isolated_dirs, monkeypatch) -> None:
        """The whole point: a maintenance outage costs no operator /approve."""
        monkeypatch.delenv("AWM_SSH_SLOT_PEER", raising=False)
        svc = SSHService()
        cfg = resolve_host("fir")
        alerts: list[str] = []
        monkeypatch.setattr(svc, "_alert",
                            lambda t: alerts.append(t) or _noop_alert(t))

        async def _call(_peer, service, fn, args=None, **kw):
            if service == "2fa" and fn == "burst":
                return {"status": "started", "transactions_seen": 4}
            if service == "2fa" and fn == "status":
                return {"transactions_seen": 4}      # Duo heard nothing
            return {}

        async def _hang_after_spawn(c, marker, rec):
            rec.spawned = True
            rec.twofa_seen_at_arm = 4
            raise ssh_service._AttemptFailed("no master")

        monkeypatch.setattr(ssh_service.gatewayclient, "call_maybe_peer", _call)
        monkeypatch.setattr(svc, "_check_master", _false)
        monkeypatch.setattr(svc, "_attempt_master", _hang_after_spawn)

        result = await svc._connect_via_local_arbiter(cfg)

        assert result["status"] == "error"
        assert svc._read_lock(cfg) is None, "no hold — Duo was never presented a login"
        assert alerts == [], "and therefore nothing to page the operator about"
        assert "fir" not in svc._leased, "the slot is freed for the next attempt"
