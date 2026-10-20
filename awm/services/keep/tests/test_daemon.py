"""The keeper's decisions, against a fake host: no systemd, no daemon, no signals."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from awm.keep import daemon
from awm.keep.daemon import Daemon, Keeper

UNIT = f"{daemon.UNIT}.service"


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


@dataclass
class FakeHost(daemon.Host):
    clock: float = 10_000.0
    has_bus: bool = True
    unit_state: dict[str, Any] = field(default_factory=lambda: {"LoadState": "not-found",
                                                                "ActiveState": "inactive"})
    running: Daemon | None = None
    calls: list[tuple] = field(default_factory=list)
    hold_state: dict | None = None
    linger_state: bool | None = True
    tail: str = ""

    def now(self) -> float:
        return self.clock

    def bus(self):
        return {"XDG_RUNTIME_DIR": "/run/user/1"} if self.has_bus else None

    def daemon(self, main_pid):
        return self.running

    def systemctl(self, bus, *args):
        self.calls.append(("systemctl", *args))
        return _ok()

    def unit(self, bus):
        return dict(self.unit_state)

    def run_unit(self, bus, argv):
        self.calls.append(("systemd-run", *argv))
        return _ok()

    def terminate(self, pid):
        self.calls.append(("kill", pid))

    def log_tail(self):
        return self.tail

    def linger(self):
        return self.linger_state

    def enable_linger(self):
        self.calls.append(("enable-linger",))
        return None

    def held(self):
        return self.hold_state

    def set_held(self, hold):
        self.hold_state = hold


@pytest.fixture(autouse=True)
def _allowed(monkeypatch):
    monkeypatch.setattr(daemon, "enabled", lambda: None)
    monkeypatch.setattr(Keeper, "holds_lock", lambda self: True)
    monkeypatch.delenv("AWM_KEEP_MEMORY_HIGH", raising=False)
    monkeypatch.delenv("AWM_KEEP_MEMORY_MAX", raising=False)


def _daemon(origin="service", unit=UNIT, started_at=9_000.0, pid=42) -> Daemon:
    return Daemon(pid=pid, origin=origin, unit=unit, started_at=started_at,
                  version="2.1.304", control=True)


def test_no_daemon_launches_the_unit_with_backoff_and_oom_properties():
    host = FakeHost()
    out = Keeper(host).reconcile()
    assert out["action"] == "launched"
    argv = next(c for c in host.calls if c[0] == "systemd-run")
    assert f"--unit={daemon.UNIT}" in argv
    for prop in ("KillMode=process", "Restart=always", "RestartSteps=6",
                 "RestartMaxDelaySec=10min", "StartLimitIntervalSec=0",
                 "OOMPolicy=continue", "OOMScoreAdjust=-200"):
        assert f"--property={prop}" in argv
    assert argv[-4:] == ("daemon", "run", "--origin", "service")


def test_memory_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("AWM_KEEP_MEMORY_HIGH", "2G")
    monkeypatch.setenv("AWM_KEEP_MEMORY_MAX", "3G")
    host = FakeHost()
    Keeper(host).reconcile()
    argv = next(c for c in host.calls if c[0] == "systemd-run")
    assert "--property=MemoryHigh=2G" in argv and "--property=MemoryMax=3G" in argv


def test_a_dead_unit_that_still_holds_sessions_is_started_in_place():
    host = FakeHost(unit_state={"LoadState": "loaded", "ActiveState": "inactive"})
    out = Keeper(host).reconcile()
    assert out["action"] == "launched"
    assert ("systemctl", "start", "--no-block", UNIT) in host.calls
    assert not any(c[0] == "systemd-run" for c in host.calls)


def test_a_kept_daemon_is_left_alone():
    host = FakeHost(running=_daemon(), unit_state={"ActiveState": "active", "NRestarts": 0})
    assert Keeper(host).reconcile()["action"] == "none"
    assert host.calls == []


def test_restart_count_is_reset_once_the_daemon_has_settled():
    young = FakeHost(running=_daemon(started_at=10_000.0 - 60),
                     unit_state={"ActiveState": "active", "NRestarts": 3})
    assert Keeper(young).reconcile()["action"] == "none"
    old = FakeHost(running=_daemon(started_at=10_000.0 - daemon.SETTLE_S - 1),
                   unit_state={"ActiveState": "active", "NRestarts": 3})
    assert Keeper(old).reconcile()["action"] == "settled"
    assert ("systemctl", "reset-failed", UNIT) in old.calls


def test_a_unit_waiting_out_its_backoff_is_not_relaunched():
    host = FakeHost(unit_state={"LoadState": "loaded", "ActiveState": "activating",
                                "SubState": "auto-restart", "NRestarts": 5})
    out = Keeper(host).reconcile()
    assert out["action"] == "restarting"
    assert host.calls == []


def test_ensure_cuts_the_backoff_short_at_most_once_a_minute():
    host = FakeHost(unit_state={"LoadState": "loaded", "ActiveState": "activating",
                                "SubState": "auto-restart", "NRestarts": 5})
    keeper = Keeper(host)
    assert keeper.reconcile(kick=True)["action"] == "kicked"
    assert keeper.reconcile(kick=True)["action"] == "restarting"
    host.clock += daemon.KICK_EVERY_S
    assert keeper.reconcile(kick=True)["action"] == "kicked"


def test_a_transient_daemon_with_no_unit_gets_the_kept_one_launched_beside_it():
    host = FakeHost(running=_daemon(origin="transient", unit="session-1883.scope"))
    out = Keeper(host).reconcile()
    assert out["action"] == "launched"
    assert not any(c[0] == "kill" for c in host.calls)


def test_a_transient_daemon_that_will_not_yield_is_terminated_after_the_grace():
    host = FakeHost(running=_daemon(origin="transient", unit="session-1883.scope"),
                    unit_state={"LoadState": "loaded", "ActiveState": "activating",
                                "SubState": "auto-restart"})
    keeper = Keeper(host)
    assert keeper.reconcile()["action"] == "displacing"
    host.clock += daemon.DISPLACE_S - 1
    assert keeper.reconcile()["action"] == "displacing"
    host.clock += 2
    assert keeper.reconcile()["action"] == "displaced"
    assert ("kill", 42) in host.calls


def test_a_service_daemon_in_a_hand_written_unit_is_adopted():
    host = FakeHost(running=_daemon(unit="claude-daemon.service"))
    out = Keeper(host).reconcile()
    assert out == {"action": "none", "pid": 42, "foreign_unit": "claude-daemon.service"}
    assert host.calls == []


def test_held_means_hands_off():
    host = FakeHost(hold_state={"since": 1, "reason": "rehearsal"})
    assert Keeper(host).reconcile()["action"] == "held"
    assert host.calls == []


def test_hold_stops_the_unit_and_release_starts_it():
    host = FakeHost(running=_daemon(), unit_state={"ActiveState": "active", "MainPID": 42})
    keeper = Keeper(host)
    out = keeper.hold("rehearsal")
    assert out["held"] and ("systemctl", "stop", UNIT) in host.calls
    assert not any(c[0] == "kill" for c in host.calls)
    host.running = None
    host.unit_state = {"LoadState": "not-found", "ActiveState": "inactive"}
    out = keeper.release()
    assert out["held"] is False and out["action"] == "launched"


def test_hold_terminates_a_daemon_outside_the_unit():
    host = FakeHost(running=_daemon(origin="transient", unit="session-1883.scope"))
    Keeper(host).hold("rehearsal")
    assert ("kill", 42) in host.calls


def test_no_user_manager_launches_nothing_and_tries_linger_once():
    host = FakeHost(has_bus=False, linger_state=False)
    keeper = Keeper(host)
    out = keeper.reconcile()
    assert out["action"] == "no-manager"
    keeper.reconcile()
    assert host.calls == [("enable-linger",)]


def test_ensure_reports_daemon_down_when_it_cannot_run(monkeypatch):
    host = FakeHost(hold_state={"since": 1, "reason": "rehearsal"})
    out = Keeper(host).ensure(timeout=0.1)
    assert out["ok"] is False and out["reason"] == "daemon_down" and out["action"] == "held"


def test_ensure_returns_once_the_daemon_answers():
    host = FakeHost(running=_daemon(), unit_state={"ActiveState": "active"})
    out = Keeper(host).ensure(timeout=0.1)
    assert out["ok"] is True and out["pid"] == 42


LOG = """\
[2026-10-19T21:34:28.606Z] [supervisor] shutting down (cause=upgrade, uptime=7322s, leases=0, live_workers=2)
[2026-10-19T21:34:39.307Z] [supervisor] ─── daemon start ─── version=2.1.304 pid=299292 origin=service
[2026-10-19T22:00:00.000Z] [bg] something else
[2026-10-20T02:40:43.397Z] [supervisor] ─── daemon start ─── version=2.1.304 pid=3790938 origin=transient
[2026-10-20T03:00:00.000Z] [supervisor] shutting down (cause=signal, uptime=1157s, leases=1, live_workers=15)
"""


def test_history_names_why_each_daemon_before_a_start_stopped():
    events = daemon.history(LOG)
    assert events[0]["after"] == {"at": "2026-10-19T21:34:28.606Z", "cause": "upgrade",
                                  "uptime_s": 7322}
    assert events[0]["down_s"] == 11
    assert events[1]["pid"] == 3790938 and events[1]["after"]["cause"] == "killed"
    assert events[2]["pid"] is None and events[2]["after"]["cause"] == "signal"


def test_status_reports_the_last_stop():
    host = FakeHost(tail=LOG)
    out = Keeper(host).status()
    assert out["running"] is False and out["last_stop"]["cause"] == "signal"


def test_parse_show():
    out = daemon.parse_show("MainPID=0\nNRestarts=4\nResult=oom-kill\nActiveState=active\n")
    assert out["MainPID"] == 0 and out["NRestarts"] == 4 and out["Result"] == "oom-kill"


def test_daemon_env_puts_local_bin_and_mamba_first(monkeypatch, tmp_path):
    monkeypatch.setenv("MAMBA_ROOT_PREFIX", str(tmp_path))
    monkeypatch.setenv("AWM_WORKSPACE", "/opt/awm")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leak")
    env = daemon.daemon_env()
    parts = env["PATH"].split(":")
    assert parts[0].endswith("/.local/bin") and parts[1] == str(tmp_path / "condabin")
    assert env["AWM_WORKSPACE"] == "/opt/awm" and "CLAUDE_CODE_SESSION_ID" not in env


def test_manifest_projects_into_the_reflection_tool():
    from awm.keep.hub_adapter import API_MANIFEST, HANDLERS
    for fn in API_MANIFEST["functions"]:
        assert fn["tool"] == f"reflection_daemon_{fn['name']}"
        assert fn["name"] in HANDLERS
