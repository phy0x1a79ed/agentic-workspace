"""The supervisor against a stub gunicorn: spawn, probe, respawn, stop, and the
environment the internet-facing child is allowed to see."""

from __future__ import annotations

import json
import os
import socket
import sys
import textwrap
import time

import pytest

from awm.engcyc import portal

pytestmark = [pytest.mark.smoke]

STUB = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    with open(os.path.join(os.environ["ENGCYC_STATE"], "child-env.json"), "w") as f:
        json.dump({{"env": dict(os.environ), "argv": sys.argv[1:], "cwd": os.getcwd()}}, f)
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({{"Genome": "text", "Mean Completeness": "numeric"}}).encode()
            self.send_response(200 if self.path == "/api/v1/get_fields" else 404)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    host, port = os.environ["ENGCYC_BIND"].rsplit(":", 1)
    HTTPServer((host, int(port)), H).serve_forever()
""")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    state, checkout = tmp_path / "state", tmp_path / "portal"
    (checkout / "src" / "server").mkdir(parents=True)
    (checkout / "src" / "server" / "wsgi.py").write_text("")
    state.mkdir()
    stub = tmp_path / "bin" / "gunicorn"
    stub.parent.mkdir()
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    (state / "gunicorn-bin").write_text(f"{stub}\n")
    monkeypatch.setattr(portal, "STATE_DIR", state)
    monkeypatch.setattr(portal, "PORTAL_DIR", checkout)
    monkeypatch.setattr(portal, "WSGI_FILE", checkout / "src" / "server" / "wsgi.py")
    monkeypatch.setattr(portal, "GUNICORN_BIN_FILE", state / "gunicorn-bin")
    monkeypatch.setattr(portal, "LOG_FILE", state / "engcyc.log")
    monkeypatch.setattr(portal, "BIND", f"127.0.0.1:{_free_port()}")
    monkeypatch.setattr(portal, "START_TIMEOUT_S", 15.0)
    monkeypatch.setenv("ENGCYC_DATA", "/data")
    monkeypatch.setenv("AWM_BOARD_TOKEN", "must-not-leak")
    sup = portal.Supervisor()
    yield sup, state
    sup.stop(timeout=5)


def test_no_checkout_is_idle_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "WSGI_FILE", tmp_path / "absent" / "wsgi.py")
    monkeypatch.setattr(portal, "PORTAL_DIR", tmp_path / "absent")
    sup = portal.Supervisor()
    assert "no portal checkout" in portal.installed()
    assert sup.reconcile() == {"action": "none"}
    assert sup.snapshot()["running"] is False


def test_start_serves_and_probes(rig):
    sup, state = rig
    snap = sup.start()
    assert snap["running"] and snap["error"] is None
    assert snap["probe"]["status"] == 200 and snap["probe"]["fields"] == 2
    seen = json.loads((state / "child-env.json").read_text())
    assert seen["argv"] == ["-c", "server/gunicorn.conf.py", "server.wsgi"]
    assert seen["cwd"].endswith("portal/src")


def test_the_child_sees_only_engcyc_variables_and_path(rig):
    sup, state = rig
    sup.start()
    env = json.loads((state / "child-env.json").read_text())["env"]
    assert "AWM_BOARD_TOKEN" not in env
    assert env["ENGCYC_DATA"] == "/data"
    assert env["ENGCYC_STATE"] == str(state)
    # A process may add a few of its own (PWD, LC_CTYPE); none come from us.
    assert {k for k in env if not k.startswith("ENGCYC_")} <= {"PATH", "PWD", "LC_CTYPE", "SHLVL", "_"}


def test_a_dead_child_is_respawned(rig):
    sup, _ = rig
    pid = sup.start()["pid"]
    os.killpg(os.getpgid(pid), 9)
    deadline = time.time() + 5
    while sup.snapshot()["running"] and time.time() < deadline:
        time.sleep(0.1)
    assert sup.reconcile()["action"] == "respawned"
    sup._await_ready()
    snap = sup.snapshot()
    assert snap["pid"] != pid and snap["probe"]["status"] == 200


def test_a_stopped_portal_stays_stopped(rig):
    sup, _ = rig
    sup.start()
    assert sup.stop()["action"] == "stopped"
    assert sup.reconcile() == {"action": "none"}
    assert sup.snapshot()["running"] is False
