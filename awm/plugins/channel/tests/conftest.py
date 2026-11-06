"""A real tether relay on loopback, the public host's relay service beside it, and
member nodes that reach it through a stand-in for its door, with their pushes captured.

The tether relay is the binary the tether repository builds. ``AWM_TETHER_RELAY_BIN``
names one, else the tests look in each local tether checkout's cargo target.

The door stand-in calls the relay plugin's own handlers as the core's door
stamps them: the calling node as ``peer:<node>`` and its service as trusted.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from awm import gatewayclient
from awm.channel import hub_adapter
from awm.channel.relayclient import RelayClient, RelayError
from awm.gatewayclient import holders
from awm.gatewayclient.push import Pusher

BEARER = "a-test-bearer-nothing-guards"
NODE = "testnode"
HOLDER = {"node": NODE, "session": "cc" + "a" * 32, "job": "job-aaaa",
          "name": "tester", "level": "trusted"}
OTHER = {"node": NODE, "session": "cc" + "b" * 32, "job": "job-bbbb",
         "name": "someone-else", "level": "cooperative"}
#: A session on a second member node.
FAR = {"node": "farnode", "session": "cc" + "c" * 32, "job": "job-cccc", "name": "far",
       "level": "trusted"}


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
    """The tether relay binary's base URL."""
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


@pytest.fixture(autouse=True)
def no_outbound(monkeypatch):
    """Nothing in these tests calls through a gateway: the relay never calls out
    (fleet.md R79), and a member node reaches the relay only through the door below."""
    def refuse(*a, **kw):
        raise AssertionError(f"an outbound gateway call: {a}")

    monkeypatch.setattr(gatewayclient, "call", refuse)
    monkeypatch.setattr(gatewayclient, "call_sync", refuse)


class Host:
    """The public host: the relay service over the tether relay, with its state on disk."""

    def __init__(self, base: str, state: Path, lease_s: float = 180.0) -> None:
        self.base, self.state, self.lease_s = base, state, lease_s
        self.service = None

    def start(self):
        from awm.relay import hub_adapter as relay_adapter
        from awm.relay.rooms import Rooms

        self.service = relay_adapter.build(self.state, rooms=Rooms(self.base, BEARER, link=self.base),
                                           lease_s=self.lease_s)
        relay_adapter.RELAY = self.service
        self.service.resume()
        return self.service

    async def stop(self) -> None:
        if self.service is not None:
            import asyncio

            await asyncio.wait_for(self.service.stop(), 5)
            await self.service.rooms.aclose()
            self.service = None

    def files(self) -> list[Path]:
        return [p for p in self.state.rglob("*") if p.is_file()]


class Door:
    """deneb's door as one member node dials it: the node's service, stamped trusted."""

    def __init__(self, node: str) -> None:
        self.node = node
        self.calls: list[str] = []

    async def __call__(self, verb: str, args: dict, timeout: float):
        from awm.relay import hub_adapter as relay_adapter
        from awm.relay.service import RelayRefused

        self.calls.append(verb)
        stamped = {**args, "_caller_trust": "trusted"}
        try:
            return await relay_adapter.HANDLERS[verb](stamped, f"peer:{self.node}")
        except RelayRefused as exc:
            raise RelayError(str(exc)) from exc


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


class Sessions:
    """The sessions running on one node. Set ``ids`` to None for "cannot tell"."""

    def __init__(self, ids: set[str]) -> None:
        self.ids: set[str] | None = ids

    def __call__(self) -> set[str] | None:
        return None if self.ids is None else set(self.ids)


@pytest.fixture
def pushed() -> Captured:
    return Captured()


@pytest.fixture
def sessions() -> Sessions:
    return Sessions({HOLDER["session"], OTHER["session"]})


@pytest.fixture
def machines(tmp_path):
    """The machine kind, on a stream and mark of its own, with no daemon."""
    from awm.channel.machines import Machines
    from awm.tether import stream

    mark = stream.Mark(tmp_path / "stream.json")
    return Machines(journal=stream.Journal(), mark=mark,
                    logs=stream.SessionLogs(tmp_path / "sessions", mark))


@pytest.fixture
async def host(tmp_path, relay):
    h = Host(relay, tmp_path / "deneb")
    h.start()
    yield h
    await h.stop()


def member_node(tmp_path: Path, node: str, pushed: Captured, sessions: Sessions,
                machines=None, feed_wait_s: float = 2.0):
    """One member node's channel service, reaching the relay through its door."""
    import asyncio

    from awm.gatewayclient import spawn_supervised

    door = Door(node)
    built = hub_adapter.build(tmp_path / node, relay=RelayClient(door),
                              pusher=Pusher(send=pushed.send), list_sessions=sessions,
                              machines=machines, feed_wait_s=feed_wait_s, here=node)
    built.door = door
    built.feeding = spawn_supervised(f"feed-{node}", built.feed_forever, respawn_delay=0.1)
    built.resume()
    assert isinstance(built.feeding, asyncio.Task)
    return built


async def stop_node(built) -> None:
    import asyncio

    built.feeding.cancel()
    await asyncio.gather(built.feeding, return_exceptions=True)
    # Bounded, so a follow loop that ignores its cancel fails the test.
    await asyncio.wait_for(built.stop(), 5)


@pytest.fixture
async def channels(tmp_path, host, pushed, sessions, machines, monkeypatch):
    monkeypatch.setattr(holders.config, "node_name", lambda: NODE)
    built = member_node(tmp_path, NODE, pushed, sessions, machines)
    yield built
    await stop_node(built)


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
