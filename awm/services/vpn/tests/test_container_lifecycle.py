"""How `up` copes with a slow docker daemon and a dead exit.

On altair the docker CLI has outlived its 30s cap several times (2026-10-06,
-14, -16) while the daemon carried on, so `up` reported failure on an exit that
came up anyway, and the next `up` hit a name conflict. And every dead exit was
removed with its logs, the only record of why the tunnel dropped.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]


class FakeDocker:
    """A docker CLI stand-in: one container's state, and every call made."""

    def __init__(self, state: str = "", *, slow: tuple[str, ...] = (),
                 run_starts: bool = True) -> None:
        self.state = state
        self.slow = set(slow)
        self.run_starts = run_starts
        self.calls: list[list[str]] = []

    def __call__(self, args, *, env=None, timeout=30.0, check=True):
        from awm.vpn import container
        self.calls.append(list(args))
        verb = args[0]
        if verb in self.slow:
            self.slow.discard(verb)
            if verb == "run" and self.run_starts:
                self.state = "running"
            raise container.DockerTimeout(f"docker {verb} timed out after {timeout}s")
        out = ""
        if verb == "ps":
            want_all = "-aq" in args
            if self.state == "running" or (want_all and self.state):
                out = "abc123\n"
        elif verb == "inspect":
            if not self.state:
                return subprocess.CompletedProcess(args, 1, "", "No such object")
            out = (self.state if args[2] == "{{.State.Status}}"
                   else f"status={self.state} exit=1 started=a finished=b")
        elif verb == "rm":
            self.state = ""
        elif verb == "run":
            self.state = "running" if self.run_starts else "exited"
        elif verb == "logs":
            out = "Session terminated by server\n"
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture()
def harness(monkeypatch):
    from awm.vpn import container

    rows: dict = {}

    class _DAO:
        def upsert(self, profile, **kw):
            rows[profile] = kw

        def get(self, profile):
            return rows.get(profile)

        def delete(self, profile):
            rows.pop(profile, None)

    monkeypatch.setattr(container.dao, "VpnDAO", _DAO)
    monkeypatch.setattr(container.config, "load_profile", lambda p: SimpleNamespace(
        twofa_device="", env={}, image="awm-vpn-ubc"))
    monkeypatch.setattr(container, "leak_check", lambda name: ("1.2.3.4", True))
    monkeypatch.setattr(container.time, "sleep", lambda s: None)
    monkeypatch.setattr(container.shutil, "which", lambda b: "/usr/bin/docker")

    def _install(fake: FakeDocker) -> FakeDocker:
        monkeypatch.setattr(container, "_docker", fake)
        return fake

    return container, _install, rows


def test_a_run_that_outlives_the_cli_is_judged_by_what_the_daemon_did(harness):
    container, install, rows = harness
    docker = install(FakeDocker(slow=("run",)))

    desc = container.up("ubc")

    assert desc["status"] == "up"
    assert rows["ubc"]["status"] == "up"
    assert not any(c[0] == "rm" for c in docker.calls), \
        "an exit that came up must not be torn down for the CLI's timeout"


def test_a_dead_exit_leaves_its_last_words_in_the_log(harness, caplog):
    container, install, _ = harness
    install(FakeDocker(state="exited"))

    with caplog.at_level("WARNING", logger="awm.vpn.container"):
        container.up("ubc")

    assert "Session terminated by server" in caplog.text
    assert "status=exited" in caplog.text


def test_a_remove_that_never_finishes_says_so(harness):
    container, install, _ = harness

    class _StuckRm(FakeDocker):
        def __call__(self, args, **kw):
            if args[0] == "rm":
                self.calls.append(list(args))
                raise container.DockerTimeout("docker rm -f timed out after 60.0s")
            return super().__call__(args, **kw)

    install(_StuckRm(state="exited"))

    with pytest.raises(container.VpnError, match="still there"):
        container.up("ubc")


def test_a_remove_that_timed_out_but_finished_carries_on(harness):
    container, install, _ = harness

    class _RmLandsLate(FakeDocker):
        def __call__(self, args, **kw):
            if args[0] == "rm" and self.state:
                self.calls.append(list(args))
                self.state = ""     # gone by the time anyone looks
                raise container.DockerTimeout("docker rm -f timed out after 60.0s")
            return super().__call__(args, **kw)

    install(_RmLandsLate(state="exited"))

    assert container.up("ubc")["status"] == "up"


def test_concurrent_ups_dial_once(harness):
    """Two connects at once must not both dial the same container name."""
    import threading

    container, install, _ = harness
    docker = install(FakeDocker())
    barrier = threading.Barrier(2)
    results: list = []

    def _go():
        barrier.wait()
        results.append(container.up("ubc")["status"])

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["up", "up"]
    assert sum(1 for c in docker.calls if c[0] == "run") == 1


def test_the_gateway_gives_up_its_full_budget():
    """The gateway caps an RPC at 30s unless the manifest says otherwise."""
    from awm.vpn import container, hub_adapter

    up = next(f for f in hub_adapter.API_MANIFEST["functions"] if f["name"] == "up")
    assert up["timeout"] == container.UP_BUDGET_S
    assert container.UP_BUDGET_S > container._UP_TIMEOUT_S + 2 * 30.0
