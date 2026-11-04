"""awm.gatewayclient.proc: a child that dies with its parent, and the user manager's env."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from awm.gatewayclient import proc


def _ends(pid: int, within: float = 10.0) -> bool:
    """Whether ``pid`` exits (or is left a zombie) within ``within`` seconds."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/stat") as fh:
                if fh.read().rsplit(")", 1)[-1].split()[0] == "Z":
                    return True
        except (ProcessLookupError, FileNotFoundError):
            return True
        time.sleep(0.05)
    os.kill(pid, 9)
    return False


def test_a_child_runs_in_its_own_session():
    child = subprocess.Popen([sys.executable, "-c", "import os; print(os.getsid(0))"],
                             stdout=subprocess.PIPE, text=True, preexec_fn=proc.child_preexec)
    out, _ = child.communicate(timeout=10)
    assert int(out) == child.pid


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux only")
def test_a_child_dies_with_its_parent(tmp_path):
    pidfile = tmp_path / "pid"
    parent = subprocess.Popen([sys.executable, "-c", (
        "import subprocess, sys, time\n"
        "from awm.gatewayclient import proc\n"
        "c = subprocess.Popen(['sleep', '60'], preexec_fn=proc.child_preexec)\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n")], env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    deadline = time.monotonic() + 10
    while not (pidfile.exists() and pidfile.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    parent.kill()
    parent.wait()
    assert _ends(pid), "the child outlived its parent"


def test_no_user_manager_without_systemd_run(monkeypatch):
    monkeypatch.setattr(proc.shutil, "which", lambda name: None)
    assert proc.user_manager_env() is None


def test_the_user_manager_env_names_the_bus(monkeypatch, tmp_path):
    monkeypatch.setattr(proc.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert proc.user_manager_env() is None  # no bus socket yet
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(tmp_path / "bus"))
        assert proc.user_manager_env() == {
            "XDG_RUNTIME_DIR": str(tmp_path),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={tmp_path}/bus"}


def test_signal_group_reaches_the_grandchild():
    child = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE,
                             text=True, preexec_fn=proc.child_preexec)
    grandchild = int(child.stdout.readline())
    proc.signal_group(child, 15)
    child.wait(timeout=10)
    assert _ends(grandchild), "the grandchild outlived the group signal"


def test_signal_group_on_a_reaped_child_is_quiet():
    child = subprocess.Popen(["true"], preexec_fn=proc.child_preexec)
    child.wait()
    proc.signal_group(child, 15)
