"""The operator daemon outlives the adapter, and is replaced only on purpose.

Every deploy restarts this service, and a daemon parented to it ended every
live session each time. These tests run a stand-in daemon that answers
``status`` on the control socket, with no user systemd manager, which is the
fallback path: own session, no death signal.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
import time

import pytest

from awm.tether import daemon, paths

pytestmark = [pytest.mark.unit]

FAKE = textwrap.dedent("""\
    #!{python}
    import json, os, signal, socket, sys
    path = os.environ["TETHER_CONTROL_SOCKET"]
    sessions = os.environ.get("FAKE_TETHER_SESSIONS")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(8)
    def bye(*_):
        os.unlink(path)
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    while True:
        conn, _ = server.accept()
        conn.makefile().readline()
        live = json.load(open(sessions)) if sessions and os.path.exists(sessions) else []
        conn.sendall(json.dumps({{"ok": True, "pid": os.getpid(),
                                  "sessions": live}}).encode() + b"\\n")
        conn.close()
""")


@pytest.fixture
def binary(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "tether-operator"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(0o755)
    (bin_dir / "VERSION").write_text("v1\n")
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)
    monkeypatch.setattr(paths, "BIN_DIR", bin_dir)
    monkeypatch.setattr(paths, "OPERATOR_BIN", exe)
    monkeypatch.setattr(paths, "VERSION_FILE", bin_dir / "VERSION")
    monkeypatch.setattr(daemon, "START_TIMEOUT_S", 10.0)
    sessions = tmp_path / "sessions.json"
    monkeypatch.setenv("FAKE_TETHER_SESSIONS", str(sessions))
    started: list[int] = []
    yield exe, sessions, started
    for pid in started + [_pid()]:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _pid() -> int | None:
    state, report = daemon.probe(timeout=1)
    return report.get("pid") if state == "answering" else None


def _launch(binary) -> int:
    exe, _, started = binary
    child = daemon.Adopted()
    assert child.reconcile()["action"] == "started"
    pid = _pid()
    started.append(pid)
    return pid


def test_the_operator_role_adopts_and_the_relay_role_parents(monkeypatch):
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)
    assert isinstance(daemon.for_role(), daemon.Adopted)
    monkeypatch.setattr(paths, "ROLE", paths.RELAY)
    assert isinstance(daemon.for_role(), daemon.Child)


def test_with_nothing_running_a_daemon_is_launched_in_its_own_session(binary):
    pid = _launch(binary)

    assert pid is not None
    assert os.getsid(pid) == pid, "it must not share the adapter's session"
    launched = json.loads(paths.LAUNCH_FILE.read_text())
    assert launched["pid"] == pid
    assert launched["release"] == "v1"


def test_a_new_adapter_attaches_to_the_running_daemon_instead_of_spawning(binary):
    pid = _launch(binary)

    first = daemon.Adopted()
    first.shutdown()
    assert _pid() == pid, "shutting the adapter down stopped the daemon"

    second = daemon.Adopted()
    assert second.reconcile() == {"action": "none"}
    assert _pid() == pid
    snap = second.snapshot()
    assert snap["adopted"] is True
    assert snap["pid"] == pid
    assert snap["running"] is True


def test_a_new_release_waits_for_the_live_session_to_end(binary):
    exe, sessions, started = binary
    pid = _launch(binary)
    sessions.write_text(json.dumps([{"slot": 7, "phase": "open"}]))
    paths.VERSION_FILE.write_text("v2\n")

    child = daemon.Adopted()
    answer = child.reconcile()

    assert answer["action"] == "deferred"
    assert "v1" in answer["reason"] and "v2" in answer["reason"]
    assert _pid() == pid, "a pin change cut a live session"
    assert child.snapshot()["replace_pending"] == answer["reason"]

    sessions.write_text(json.dumps([{"slot": 7, "phase": "ended"}]))
    replaced = child.reconcile()
    started.append(_pid())

    assert replaced["action"] == "replaced"
    assert _pid() not in (None, pid)
    assert json.loads(paths.LAUNCH_FILE.read_text())["release"] == "v2"
    assert child.reconcile() == {"action": "none"}


def test_restart_is_refused_while_a_session_is_live_unless_forced(binary):
    exe, sessions, started = binary
    pid = _launch(binary)
    sessions.write_text(json.dumps([{"slot": 3, "phase": "open"}]))

    child = daemon.Adopted()
    refused = child.restart()
    assert refused["ok"] is False
    assert refused["sessions"] == [3]
    assert _pid() == pid

    forced = child.restart(force=True)
    started.append(_pid())
    assert forced["action"] == "started"
    assert _pid() not in (None, pid)


def test_a_daemon_that_will_not_start_backs_off(binary, monkeypatch):
    exe, _, _ = binary
    exe.write_text("#!/bin/sh\nexit 3\n")
    monkeypatch.setattr(daemon, "START_TIMEOUT_S", 1.0)

    child = daemon.Adopted()
    assert child.reconcile()["action"] == "failed"
    assert child.reconcile()["action"] == "backoff"
    assert "did not answer" in child.snapshot()["error"]


def test_a_daemon_that_dies_young_backs_off_rather_than_cycling(binary):
    exe, _, started = binary
    child = daemon.Adopted()
    assert child.reconcile()["action"] == "started"
    pid = _pid()
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while _pid() is not None and time.monotonic() < deadline:
        time.sleep(0.05)

    assert child.reconcile()["action"] == "backoff"


def test_the_environment_file_survives_quotes_dollars_and_newlines():
    text = daemon._environment_file({
        "PLAIN": "x", "QUOTED": 'a "b" \\c $d', "LINES": "one\ntwo",
        "INVOCATION_ID": "gateway's own", "not-a-name": "skipped",
    })

    assert 'PLAIN="x"' in text
    assert 'QUOTED="a \\"b\\" \\\\c $d"' in text
    assert 'LINES="one\ntwo"' in text
    assert "INVOCATION_ID" not in text
    assert "not-a-name" not in text

