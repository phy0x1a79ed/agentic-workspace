"""Adoption is the behaviour that makes an awm deploy safe for live work.

If `start` ever decides to launch a second daemon beside a running one, or
`reconcile` ever stops one that is merely busy, the cost lands on somebody
mid-analysis. Both are pinned here, along with the origin derivation the
workbench's WebSocket origin check depends on — that one fails closed and
silently, so it is worth a test rather than a comment.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from awm.claude_science import daemon

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def sup():
    return daemon.Supervisor(main_front_port=12201, sandbox_front_port=12202)


def _running(**over):
    base = {"running": True, "pid": 4242, "version": "0.1.27", "port": 12203}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------

def test_start_adopts_a_running_daemon_and_launches_nothing(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", _running)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)

    snap = sup.start()
    assert snap["adopted"] is True
    assert snap["pid"] == 4242
    # Started-by-us is a different fact from running, and status must not lie
    # about which one it is.
    assert snap["supervised_since"] is None


# ---------------------------------------------------------------------------
# On demand: start on a request, stop when idle
# ---------------------------------------------------------------------------
#
# `status_json` never raises, so a timed-out subprocess, an OSError and garbage
# on stdout all reach `reconcile` looking exactly like a daemon that is down:
# running=False plus an `error`. Acting on that is destructive: the probe once
# reported dead while the daemon was answering HTTP 200s on either side of the
# same tick.

@pytest.mark.parametrize("error", [
    "status timed out after 30s",
    "OSError: [Errno 12] Cannot allocate memory",
    "unparseable status output: ''",
])
def test_reconcile_leaves_the_daemon_alone_on_an_inconclusive_probe(sup, monkeypatch, error):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json",
                        lambda: {"running": False, "error": error})
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    monkeypatch.setattr(daemon, "port_listening", _never_called)

    out = sup.reconcile()
    assert out["action"] == "skipped"
    assert "inconclusive" in out["reason"]


def test_reconcile_never_starts_a_daemon_that_is_down(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", lambda: {"running": False})
    monkeypatch.setattr(daemon, "port_listening", lambda port, **kw: False)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)

    assert sup.reconcile()["action"] == "down"
    assert sup.snapshot()["state"] == "idle-stopped"


def test_reconcile_believes_the_socket_over_a_clean_not_running(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", lambda: {"running": False})
    monkeypatch.setattr(daemon, "port_listening", lambda port, **kw: True)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)

    assert sup.reconcile()["reason"] == "port still listening"


def _idle_daemon(sup, monkeypatch, *, idle_for):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", _running)
    monkeypatch.setattr(daemon, "IDLE_S", 7200.0)
    stopped = []
    monkeypatch.setattr(daemon.Supervisor, "stop", lambda self: stopped.append(True))
    sup.last_activity = time.time() - idle_for
    return stopped


def test_reconcile_stops_a_daemon_idle_past_the_limit(sup, monkeypatch):
    stopped = _idle_daemon(sup, monkeypatch, idle_for=7201)
    assert sup.reconcile(busy=list)["action"] == "stopped"
    assert stopped == [True]


def test_reconcile_keeps_a_daemon_with_recent_activity(sup, monkeypatch):
    stopped = _idle_daemon(sup, monkeypatch, idle_for=7000)
    assert sup.reconcile(busy=_never_called)["action"] == "ok"
    assert stopped == []


def test_reconcile_keeps_a_daemon_with_work_running_and_restarts_the_clock(sup, monkeypatch):
    stopped = _idle_daemon(sup, monkeypatch, idle_for=9000)
    assert sup.reconcile(busy=lambda: [{"id": "c1"}])["reason"] == "work running"
    assert stopped == []
    assert time.time() - sup.last_activity < 5


def test_reconcile_does_not_stop_when_it_cannot_ask_what_is_running(sup, monkeypatch):
    stopped = _idle_daemon(sup, monkeypatch, idle_for=9000)

    def broken():
        raise RuntimeError("login failed")

    assert sup.reconcile(busy=broken)["action"] == "skipped"
    assert stopped == []


# ---------------------------------------------------------------------------
# Stale grant scans: pruned on every stop, never while the daemon runs
# ---------------------------------------------------------------------------

def _scans(path, *grants):
    import json
    doc = {"version": 1, "key": "k",
           "records": {f"1:{i}": {"path": f"{g}/x", "grant": g, "complete": True}
                       for i, g in enumerate(grants)}}
    path.write_text(json.dumps(doc))
    path.chmod(0o600)
    return path


def test_prune_keeps_only_records_of_current_rw_grants(tmp_path):
    import json
    path = _scans(tmp_path / "scans.json", "/ws/out", "/ws/projects")

    assert daemon.prune_grant_scans(path, {"/ws/out"}) == ["/ws/projects/x"]
    doc = json.loads(path.read_text())
    assert (doc["version"], doc["key"]) == (1, "k")
    assert [r["grant"] for r in doc["records"].values()] == ["/ws/out"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert daemon.prune_grant_scans(tmp_path / "absent.json", set()) == []


def _stopping(sup, monkeypatch, tmp_path, *, grants, still_running=False):
    path = _scans(tmp_path / "scans.json", "/ws/out", "/ws/projects")
    monkeypatch.setattr(daemon, "GRANT_SCANS", path)
    monkeypatch.setattr(daemon, "installed", lambda: True)
    states = iter([_running()])
    monkeypatch.setattr(daemon, "status_json", lambda: next(
        states, _running() if still_running else {"running": False}))
    monkeypatch.setattr(daemon, "_run", lambda args, **kw: type(
        "P", (), {"returncode": 0, "stderr": ""})())
    monkeypatch.setattr(daemon.Supervisor, "_rw_grants", lambda self: grants)
    sup.stop()
    return daemon.json.loads(path.read_text())["records"]


def test_stop_prunes_the_scans_of_grants_no_longer_rw(sup, monkeypatch, tmp_path):
    records = _stopping(sup, monkeypatch, tmp_path, grants={"/ws/out"})
    assert [r["grant"] for r in records.values()] == ["/ws/out"]


@pytest.mark.parametrize("grants,still_running", [(None, False),
                                                  ({"/ws/out"}, True)])
def test_stop_leaves_the_scans_alone_when_unsure(sup, monkeypatch, tmp_path,
                                                 grants, still_running):
    records = _stopping(sup, monkeypatch, tmp_path, grants=grants,
                        still_running=still_running)
    assert len(records) == 2


def test_an_adopted_daemon_starts_its_idle_clock_at_adoption(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", _running)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.last_activity is None
    assert sup.adopt()["adopted"] is True
    assert time.time() - sup.last_activity < 5


def test_adopt_launches_nothing_when_the_daemon_is_down(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", lambda: {"running": False})
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.adopt()["state"] == "idle-stopped"


def test_a_request_starts_a_stopped_daemon_and_counts_as_activity(sup, monkeypatch):
    started = []
    monkeypatch.setattr(daemon.Supervisor, "start",
                        lambda self: started.append(True) or {"running": True})
    sup.ensure_running()
    assert started == [True]
    assert time.time() - sup.last_activity < 5


def test_two_wakes_at_once_start_one_daemon(sup, monkeypatch):
    entered, release, starts = threading.Event(), threading.Event(), []

    def slow_start(self):
        starts.append(True)
        entered.set()
        release.wait(5)

    monkeypatch.setattr(daemon.Supervisor, "start", slow_start)
    assert sup.wake() is True
    assert entered.wait(5)
    assert sup.wake() is False
    release.set()
    deadline = time.time() + 5
    while sup._waking.locked() and time.time() < deadline:
        time.sleep(0.01)
    assert starts == [True]


def _never_called(*args, **kwargs):
    raise AssertionError("the supervisor must not spawn a process here")


# ---------------------------------------------------------------------------
# Where the daemon is launched — the half of "outlives us" that detaching
# does not buy. See daemon.user_manager_env.
# ---------------------------------------------------------------------------

def _capture_launch(monkeypatch, *, has_user_manager: bool):
    """Drive one start() against a dead daemon and return the argv it launched."""
    calls: list[list[str]] = []

    class _Ok:
        stdout, stderr, returncode = "", "", 0

    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(
        daemon, "user_manager_env",
        (lambda: {"XDG_RUNTIME_DIR": "/run/user/1000",
                  "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
        if has_user_manager else (lambda: None),
    )

    # Dead before the launch, alive after it — so start() takes the spawn path
    # and then its poll succeeds.
    states = iter([{"running": False}])

    def _status():
        return next(states, {"running": True, "pid": 999, "port": 12203})

    monkeypatch.setattr(daemon, "status_json", _status)

    def _run(argv, **kwargs):
        calls.append(list(argv))
        return _Ok()

    monkeypatch.setattr(daemon.subprocess, "run", _run)
    daemon.Supervisor(main_front_port=12201, sandbox_front_port=12202).start()
    return calls


def test_launch_goes_into_a_user_manager_unit_when_there_is_one(monkeypatch):
    """`systemctl restart awm` kills by cgroup, and a cgroup is inherited by
    every descendant however it detaches — so the daemon has to be started into
    a cgroup awm.service does not own, or it dies on the deploy it is meant to
    survive."""
    launch = _capture_launch(monkeypatch, has_user_manager=True)[-1]
    assert launch[0] == "systemd-run"
    assert "--user" in launch
    assert f"--unit={daemon.TRANSIENT_UNIT}" in launch
    # systemd manages it as Type=simple; self-detaching would orphan it out of
    # the very unit that is the point of this path.
    assert "--detached" not in launch
    assert "--property=Restart=no" in launch, "systemd must not race our supervision"


def test_the_name_is_cleared_before_it_is_claimed(monkeypatch):
    """A unit still deactivating from the stop half of a restart holds the name
    and makes systemd-run refuse."""
    calls = _capture_launch(monkeypatch, has_user_manager=True)
    cleanup = [c for c in calls if c[:2] == ["systemctl", "--user"]]
    assert [c[2] for c in cleanup] == ["stop", "reset-failed"]


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------

def test_origin_comes_from_the_declared_edge_url(monkeypatch):
    """Declared, not enumerated: on the production node the mesh address
    belongs to the Windows host and is invisible from inside WSL."""
    monkeypatch.delenv("CLAUDE_SCIENCE_PUBLIC_HOST", raising=False)
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.110:12100")
    assert daemon.front_origin(12201) == "https://10.74.81.110:12201"


def test_extra_origins_are_appended_and_deduped(monkeypatch):
    monkeypatch.delenv("CLAUDE_SCIENCE_PUBLIC_HOST", raising=False)
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.110:12100")
    monkeypatch.setenv(
        "CLAUDE_SCIENCE_EXTRA_ORIGINS",
        " https://172.25.181.70:12201 , https://10.74.81.110:12201/ ",
    )
    assert daemon.allowed_origins(12201) == [
        "https://10.74.81.110:12201",
        "https://172.25.181.70:12201",
    ]


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The routes behind auth's fronts, and the verbs
# ---------------------------------------------------------------------------

class _FakeSupervisor:
    def __init__(self):
        self.woken = 0
        self.ensured = 0

    def wake(self):
        self.woken += 1

    def ensure_running(self):
        self.ensured += 1

    def login_url(self):
        return "http://127.0.0.1:12203/?nonce=abc"


def _get(path, *, headers=None):
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from awm.claude_science import signin

    fake = _FakeSupervisor()
    server = ThreadingHTTPServer(("127.0.0.1", 0), signin._handler(fake))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = Request(f"http://127.0.0.1:{server.server_port}{path}", headers=headers or {})
        try:
            return urlopen(req, timeout=5).status, fake
        except HTTPError as exc:
            return exc.code, fake
    finally:
        server.shutdown()


def test_wake_needs_the_edge_header():
    status, fake = _get("/_wake")
    assert (status, fake.woken) == (404, 0)
    status, fake = _get("/_wake", headers={"x-awm-as": "user:alice"})
    assert (status, fake.woken) == (202, 1)


def test_signin_counts_as_activity_and_waits_for_the_daemon():
    status, fake = _get("/_signin", headers={"x-awm-as": "user:alice"})
    assert (status, fake.ensured) == (200, 1)
    assert _get("/_signin")[1].ensured == 0


@pytest.mark.parametrize("verb", ["url", "connector", "grants", "message", "new", "read"])
def test_a_verb_that_needs_the_daemon_starts_it_first(verb, monkeypatch):
    import asyncio

    from awm.claude_science import hub_adapter

    order = []

    class Stop(Exception):
        pass

    def ensure():
        order.append("ensure")

    def after(*a, **k):
        order.append("api")
        raise Stop

    monkeypatch.setattr(hub_adapter.SUPERVISOR, "ensure_running", ensure)
    monkeypatch.setattr(hub_adapter.SUPERVISOR, "login_url", after)
    args = {"text": "hi"}
    with pytest.raises(Stop):
        asyncio.run(hub_adapter.HANDLERS[verb](args))
    assert order == ["ensure", "api"]


@pytest.mark.parametrize("verb", ["status", "logs", "signin_url", "update_check"])
def test_a_reporting_verb_does_not_wake_the_daemon(verb, monkeypatch):
    import asyncio

    from awm.claude_science import hub_adapter

    monkeypatch.setattr(hub_adapter.SUPERVISOR, "ensure_running", _never_called)
    monkeypatch.setattr(hub_adapter.SUPERVISOR, "start", _never_called)
    monkeypatch.setattr(hub_adapter.SUPERVISOR, "snapshot", lambda: {"running": False})
    monkeypatch.setattr(hub_adapter.SUPERVISOR, "install_state", lambda: {})
    monkeypatch.setattr(hub_adapter.SUPERVISOR, "logs", lambda tail: "")
    monkeypatch.setattr(hub_adapter.SUPERVISOR, "update_check", lambda: {})
    monkeypatch.setattr(hub_adapter, "_front", lambda name: asyncio.sleep(0, {}))
    asyncio.run(hub_adapter.HANDLERS[verb]({}))


# ---------------------------------------------------------------------------
# Sign-in: a booting daemon cannot mint yet
# ---------------------------------------------------------------------------

def test_login_url_waits_out_a_booting_daemon(sup, monkeypatch):
    outputs = iter(["", "", "http://localhost:12203/?nonce=abc\n"])
    monkeypatch.setattr(daemon, "_run", lambda args, timeout: subprocess.CompletedProcess(
        args, 0, stdout=next(outputs), stderr="couldn't mint a sign-in link"))
    monkeypatch.setattr(daemon, "status_json", _running)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)

    assert sup.login_url() == "http://localhost:12203/?nonce=abc"


def test_login_url_gives_up_at_once_when_no_daemon_runs(sup, monkeypatch):
    monkeypatch.setattr(daemon, "_run", lambda args, timeout: subprocess.CompletedProcess(
        args, 4, stdout="", stderr="No daemon running."))
    monkeypatch.setattr(daemon, "status_json", lambda: {"running": False})

    with pytest.raises(RuntimeError, match="No daemon running"):
        sup.login_url()
