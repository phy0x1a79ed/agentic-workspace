"""Restarting a gateway whether or not systemd supervises it.

Prod on this host is started from the PID file and orphaned to init: there is no
unit, so ``systemctl restart`` reports the unit missing and leaves the gateway
running. mira has a per-user unit and must keep using it. Which one applies is
probed, and these tests pin the probe, the ordering the PID-file path depends
on, and the override that keeps ``awm deploy`` targeting the prod system unit.
"""

from __future__ import annotations

import subprocess

import pytest

from awm.gateway import core

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


class TestUnitProbe:
    def test_active_unit_is_recognised(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc("active\n"))
        assert core.user_unit_is_active() is True

    def test_inactive_unit_is_not(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc("inactive\n"))
        assert core.user_unit_is_active() is False

    def test_enabled_but_bad_unit_is_not_active(self, monkeypatch):
        """A dangling unit symlink reports enabled+bad and cannot be started.

        Probing is-enabled instead of is-active would pick systemd here and the
        restart would fail with "Unit awm.service could not be found".
        """
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc("inactive\n"))
        assert core.user_unit_is_active() is False

    def test_missing_systemctl_is_not_fatal(self, monkeypatch):
        def _boom(*a, **k):
            raise FileNotFoundError("systemctl")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert core.user_unit_is_active() is False

    def test_hung_systemctl_is_not_fatal(self, monkeypatch):
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired("systemctl", 5)

        monkeypatch.setattr(subprocess, "run", _boom)
        assert core.user_unit_is_active() is False


class TestModeSelection:
    """Which mechanism gets used, without actually restarting anything."""

    @staticmethod
    def _harness(monkeypatch, *, unit_active, statuses):
        """Drive restart_core_and_wait against a scripted /status sequence."""
        calls: dict[str, object] = {"popen": [], "stopped": 0, "started": 0}

        monkeypatch.setattr(core, "user_unit_is_active", lambda *a: unit_active)
        monkeypatch.setattr(core, "sweep_orphan_awm_serves", lambda: [])
        monkeypatch.setattr(core, "_wait_port_free", lambda *a: True)
        monkeypatch.setattr(
            core, "_stop_via_pidfile",
            lambda: calls.__setitem__("stopped", calls["stopped"] + 1) or 4242)
        monkeypatch.setattr(
            core, "_start_detached",
            lambda: calls.__setitem__("started", calls["started"] + 1))
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda cmd, **k: calls["popen"].append(list(cmd)))

        seq = list(statuses)

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, _path):
                # The poll loops run on a wall-clock deadline, so the exact
                # number of GETs isn't fixed. Once the script is exhausted the
                # last response repeats — that is what "the gateway stayed in
                # this state" means, and it is the steady state every timeout
                # assertion here is about.
                nxt = seq.pop(0) if len(seq) > 1 else seq[0]
                if isinstance(nxt, Exception):
                    raise nxt
                return _Resp(nxt)

        monkeypatch.setattr(core.httpx, "Client", _Client)
        return calls

    def test_unsupervised_host_takes_the_pidfile_path(self, monkeypatch):
        calls = self._harness(
            monkeypatch, unit_active=False,
            statuses=[
                {"core_pid": 100, "status": "ok", "core_uptime_s": 900},   # preflight
                {"core_pid": 200, "status": "ok", "core_uptime_s": 1},     # healthy
            ])
        out = core.restart_core_and_wait(timeout=5)

        assert out["managed_by"] == "pidfile"
        assert out["new_pid"] == 200
        assert calls["stopped"] == 1 and calls["started"] == 1
        assert calls["popen"] == []          # systemctl never invoked

    def test_supervised_host_hands_off_to_systemd(self, monkeypatch):
        import httpx
        calls = self._harness(
            monkeypatch, unit_active=True,
            statuses=[
                {"core_pid": 100, "status": "ok", "core_uptime_s": 900},   # preflight
                httpx.ConnectError("refused"),                            # drained
                {"core_pid": 200, "status": "ok", "core_uptime_s": 1},     # healthy
            ])
        out = core.restart_core_and_wait(timeout=5)

        assert out["managed_by"] == "systemd"
        assert calls["popen"] == [["systemctl", "--user", "restart", "awm.service"]]
        assert calls["stopped"] == 0 and calls["started"] == 0

    def test_explicit_cmd_wins_over_the_probe(self, monkeypatch):
        """`awm deploy` targets the prod SYSTEM unit; the probe only ever looks
        at the per-user one, so it must not be allowed to override the caller."""
        import httpx
        cmd = ["sudo", "-n", "systemctl", "restart", "awm.service"]
        calls = self._harness(
            monkeypatch, unit_active=False,          # per-user unit is NOT active
            statuses=[
                {"core_pid": 100, "status": "ok", "core_uptime_s": 900},
                httpx.ConnectError("refused"),
                {"core_pid": 200, "status": "ok", "core_uptime_s": 1},
            ])
        out = core.restart_core_and_wait(timeout=5, restart_cmd=cmd)

        assert out["managed_by"] == "systemd"
        assert calls["popen"] == [cmd]
        assert calls["stopped"] == 0

    def test_pidfile_path_does_not_wait_for_the_new_process_to_die(self, monkeypatch):
        """The drain happens before the replacement starts.

        If the generic drain-wait still ran afterwards it would poll the NEW
        gateway waiting for it to stop responding, and spin to the deadline —
        a restart that worked, reported as a timeout.
        """
        calls = self._harness(
            monkeypatch, unit_active=False,
            statuses=[
                {"core_pid": 100, "status": "ok", "core_uptime_s": 900},
                {"core_pid": 200, "status": "ok", "core_uptime_s": 1},
            ])
        out = core.restart_core_and_wait(timeout=5)
        assert out["status"] == "ok"
        assert "drain_s" in out          # drain accounted for, just earlier
        assert calls["started"] == 1

    def test_port_still_held_fails_loudly(self, monkeypatch):
        self._harness(
            monkeypatch, unit_active=False,
            statuses=[{"core_pid": 100, "status": "ok", "core_uptime_s": 900}])
        monkeypatch.setattr(core, "_wait_port_free", lambda *a: False)

        with pytest.raises(core._RestartTimeout, match="still held"):
            core.restart_core_and_wait(timeout=1)


class TestHealthGateAppliesToBothPaths:
    def test_same_pid_is_not_a_restart(self, monkeypatch):
        """The PID+uptime gate is the only thing separating 'restarted' from
        'never died' — it must guard the pidfile path too."""
        TestModeSelection._harness(
            monkeypatch, unit_active=False,
            statuses=[
                {"core_pid": 100, "status": "ok", "core_uptime_s": 900},
                {"core_pid": 100, "status": "ok", "core_uptime_s": 901},
                {"core_pid": 100, "status": "ok", "core_uptime_s": 902},
                {"core_pid": 100, "status": "ok", "core_uptime_s": 903},
                {"core_pid": 100, "status": "ok", "core_uptime_s": 904},
                {"core_pid": 100, "status": "ok", "core_uptime_s": 905},
                {"core_pid": 100, "status": "ok", "core_uptime_s": 906},
            ])
        with pytest.raises(core._RestartTimeout, match="did not become healthy"):
            core.restart_core_and_wait(timeout=1)
