"""A real tether relay on loopback, and a channel service with its push captured.

The relay is the binary the tether repository builds. ``AWM_TETHER_RELAY_BIN``
names one, else the tests look in each local tether checkout's cargo target.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from awm.channel import holders, hub_adapter
from awm.channel.relay import Rooms
from awm.gatewayclient.push import Pusher

BEARER = "a-test-bearer-nothing-guards"
HOLDER = {"node": "testnode", "session": "cc" + "a" * 32, "job": "job-aaaa",
          "name": "tester"}
OTHER = {"node": "testnode", "session": "cc" + "b" * 32, "job": "job-bbbb",
         "name": "someone-else"}
#: A holder on another node, known by its job.
FAR = {"node": "farnode", "session": "job:job-cccc", "job": "job-cccc", "name": "far"}


def _relay_bin() -> Path | None:
    named = os.environ.get("AWM_TETHER_RELAY_BIN")
    if named:
        return Path(named)
    projects = Path(__file__).resolve().parents[5].parent
    found = sorted(projects.glob("tether/*/target/*/tether-relay"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


RELAY_BIN = _relay_bin()
needs_relay = pytest.mark.skipif(RELAY_BIN is None or not RELAY_BIN.exists(),
                                 reason="needs a tether-relay binary (AWM_TETHER_RELAY_BIN)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def relay():
    port = _free_port()
    env = {"PATH": "/usr/bin:/bin", "AWM_TETHER_ISSUE_TOKEN": BEARER,
           "AWM_TETHER_PORT": str(port)}
    proc = subprocess.Popen([str(RELAY_BIN)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("the relay did not come up")
    yield base
    proc.terminate()
    _, err = proc.communicate(timeout=10)
    # The relay logs no message body, ever.
    assert b"secret-body-marker" not in err, err


class Captured:
    """Stands in for ``reflection message``: records each pushed line."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []
        self.gone: set[str] = set()

    async def send(self, node: str, job: str, line: str) -> str:
        if job in self.gone:
            return "gone"
        self.lines.append((node, job, line))
        return "ok"

    def texts(self, job: str = HOLDER["job"]) -> list[str]:
        return [line for _, j, line in self.lines if j == job]


@pytest.fixture
def pushed() -> Captured:
    return Captured()


@pytest.fixture
def live() -> dict[str, set[str] | None]:
    """The live jobs each other node reports. Edit it to end a far holder's job."""
    return {"farnode": {FAR["job"]}}


class Sessions:
    """The sessions running on this node. Set ``ids`` to None for "cannot tell"."""

    def __init__(self) -> None:
        self.ids: set[str] | None = {HOLDER["session"], OTHER["session"]}

    def __call__(self) -> set[str] | None:
        return None if self.ids is None else set(self.ids)


@pytest.fixture
def sessions() -> Sessions:
    return Sessions()


@pytest.fixture
def machines(tmp_path):
    """The machine kind, on a stream and mark of its own, with no daemon."""
    from awm.channel.machines import Machines
    from awm.channel.tether import stream

    mark = stream.Mark(tmp_path / "stream.json")
    return Machines(journal=stream.Journal(), mark=mark,
                    logs=stream.SessionLogs(tmp_path / "sessions", mark))


@pytest.fixture
async def channels(tmp_path, relay, pushed, live, sessions, machines, monkeypatch):
    async def list_jobs(node):
        return live.get(node)

    monkeypatch.setattr(holders.config, "node_name", lambda: "testnode")
    built = hub_adapter.build(tmp_path / "state", rooms=Rooms(relay, BEARER),
                              pusher=Pusher(send=pushed.send),
                              list_jobs=list_jobs, list_sessions=sessions,
                              machines=machines)
    yield built
    import asyncio
    # Bounded, so a follow loop that ignores its cancel fails the test.
    await asyncio.wait_for(built.stop(), 5)
    await built.rooms.aclose()


async def eventually(check, timeout: float = 5.0):
    """Wait until ``check()`` is truthy, and return it."""
    import asyncio

    deadline = time.time() + timeout
    while time.time() < deadline:
        got = check()
        if got:
            return got
        await asyncio.sleep(0.02)
    raise AssertionError("the condition never held")
