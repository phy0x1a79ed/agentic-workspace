"""Adoption is the behaviour that makes an awm deploy safe for live work.

If ``start`` ever launches a second dashboard beside a serving one, or
``reconcile`` ever restarts one that is merely slow, the cost lands on somebody
mid-conversation. Both are pinned here, along with the transient-unit launch —
the difference between a dashboard that survives ``systemctl restart awm`` and
one that dies with it is a cgroup, and nothing about the code makes that
visible.
"""

from __future__ import annotations

import pytest

from awm.hermes import daemon

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _never_called(*a, **k):
    raise AssertionError(f"must not be called: args={a} kwargs={k}")


@pytest.fixture()
def sup():
    return daemon.Supervisor()


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------

def test_start_adopts_a_serving_dashboard_and_launches_nothing(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "port_listening", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "unit_state", lambda: {"available": True,
                                                       "main_pid": 4242})
    monkeypatch.setattr(daemon, "dashboard_pids", lambda: [4242])
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    monkeypatch.setattr(daemon.subprocess, "Popen", _never_called)

    snap = sup.start()
    assert snap["adopted"] is True
    assert snap["pid"] == 4242
    # Started-by-us is a different fact from serving, and status must not lie
    # about which one it is.
    assert snap["supervised_since"] is None


def test_start_is_idempotent(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "port_listening", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "unit_state", lambda: {"available": False})
    monkeypatch.setattr(daemon, "dashboard_pids", list)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    monkeypatch.setattr(daemon.subprocess, "Popen", _never_called)
    assert sup.start()["adopted"] is True
    assert sup.start()["adopted"] is True


def test_start_refuses_when_the_launcher_is_absent(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: False)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    with pytest.raises(RuntimeError, match="not installed"):
        sup.start()


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def test_reconcile_leaves_a_serving_dashboard_alone(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "port_listening", lambda *a, **k: True)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.reconcile() == {"action": "ok"}
    assert sup.restarts == 0


def test_reconcile_does_nothing_when_the_launcher_is_absent(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: False)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.reconcile()["action"] == "skipped"


def test_reconcile_relaunches_a_dead_dashboard(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "port_listening", lambda *a, **k: False)
    called: list[str] = []
    monkeypatch.setattr(sup, "start", lambda: called.append("start") or {})
    assert sup.reconcile()["action"] == "respawned"
    assert called == ["start"]
    assert sup.restarts == 1


# ---------------------------------------------------------------------------
# The launch itself
# ---------------------------------------------------------------------------
#
# `systemctl restart awm` kills by control group, and a cgroup is inherited by
# every descendant however it forks. Detaching does not help; a transient unit
# of the *user* manager does, because awm.service does not own that cgroup.

def test_launch_goes_into_a_transient_user_unit(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    listening = iter([False, True])
    monkeypatch.setattr(daemon, "port_listening",
                        lambda *a, **k: next(listening, True))
    monkeypatch.setattr(daemon, "_user_manager_env",
                        lambda: {"XDG_RUNTIME_DIR": "/run/user/1000"})
    monkeypatch.setattr(daemon, "unit_state", lambda: {"available": True,
                                                       "main_pid": 7})
    monkeypatch.setattr(daemon, "dashboard_pids", lambda: [7])
    monkeypatch.setattr(daemon, "_systemctl", lambda *a, **k: None)

    seen: list[list[str]] = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda argv, **k: seen.append(argv) or _Ok())
    monkeypatch.setattr(daemon.subprocess, "Popen", _never_called)

    snap = sup.start()
    assert snap["adopted"] is False
    argv = seen[-1]
    assert argv[0] == "systemd-run"
    assert "--user" in argv and f"--unit={daemon.TRANSIENT_UNIT}" in argv
    # --skip-build: the SPA is built once at install time, and rebuilding it on
    # every respawn would turn a crash recovery into a minutes-long outage.
    assert "--skip-build" in argv and "--no-open" in argv


def test_launch_without_a_user_manager_still_starts_and_says_so(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    listening = iter([False, True])
    monkeypatch.setattr(daemon, "port_listening",
                        lambda *a, **k: next(listening, True))
    monkeypatch.setattr(daemon, "_user_manager_env", lambda: None)
    monkeypatch.setattr(daemon, "unit_state", lambda: {"available": False,
                                                       "unit": None})
    monkeypatch.setattr(daemon, "dashboard_pids", lambda: [11])
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)

    seen: list[list[str]] = []
    monkeypatch.setattr(daemon.subprocess, "Popen",
                        lambda argv, **k: seen.append(argv))

    snap = sup.start()
    assert seen and seen[0][0] == str(daemon.BIN)
    # A null unit is the tell that this dashboard will NOT survive an awm
    # restart. It has to reach status, not just a log line.
    assert snap["user_unit"]["unit"] is None
