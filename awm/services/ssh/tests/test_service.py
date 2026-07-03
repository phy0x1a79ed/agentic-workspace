from __future__ import annotations

import os
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
from awm.ssh.service import SSHService


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
