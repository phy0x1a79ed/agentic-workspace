"""The singleton door (fleet.md R98 item 2): a locked command, one call per connection."""

from __future__ import annotations

import sys

import pytest

from awm.operations import door
from awm.operations.home import Home
from awm.operations.relay import Refused, Unreachable

ME = "cc" + "1" * 32


@pytest.fixture
async def served(tmp_path, env):
    ap, store, _ = env
    path = tmp_path / "door.sock"
    server = await door.serve(Home(store, ap), path)
    yield store, path
    server.close()


def locked(path, node="mizar"):
    """What sshd runs for a door key: the forced command, here without ssh."""
    code = ("import sys; from pathlib import Path; from awm.operations.door import forced_command; "
            f"sys.exit(forced_command({node!r}, Path({str(path)!r})))")
    return door.DoorClient([sys.executable, "-c", code], timeout=20)


async def test_a_call_crosses_the_door_with_its_node(served):
    store, path = served
    client = locked(path)
    out = await client.call("task_open", {"title": "from mizar", "session_id": ME,
                                          "node": "alcor", "op_id": "mizar:1"})
    assert store.task_get(out["task_id"])["sessions"][0]["node"] == "mizar"
    again = await client.call("task_open", {"title": "from mizar", "session_id": ME,
                                            "node": "alcor", "op_id": "mizar:1"})
    assert again == out, "the op_id makes the replay a no-op"


async def test_the_door_refuses_what_is_not_a_home_verb(served):
    _, path = served
    with pytest.raises(Refused, match="does not take"):
        await locked(path).call("card_action", {"action": "close"})
    with pytest.raises(Refused):
        await locked(path).call("task_report", {"task_id": "nosuchid", "state": "done"})


async def test_a_dead_door_is_unreachable(tmp_path):
    with pytest.raises(Unreachable):
        await door.DoorClient(["/nonexistent/ssh"]).call("keys", {})
    with pytest.raises(Unreachable):
        await locked(tmp_path / "missing.sock").call("keys", {})


def test_door_argv(monkeypatch):
    monkeypatch.delenv("AWM_OPERATIONS_DOOR", raising=False)
    assert door.door_argv() is None
    monkeypatch.setenv("AWM_OPERATIONS_DOOR", "ops@alcor:2201")
    monkeypatch.setenv("AWM_OPERATIONS_DOOR_KEY", "/k")
    argv = door.door_argv()
    assert argv[:4] == ["ssh", "-T", "-p", "2201"] and argv[-2:] == ["ops@alcor", "door"]
    assert "/k" in argv


async def test_an_operator_command_needs_the_services_own_user(served, monkeypatch):
    store, path = served

    def ask(cmd: str) -> dict:
        import json
        import socket
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(path))
            s.sendall(json.dumps({"cmd": cmd}).encode() + b"\n")
            return json.loads(s.makefile().readline())

    import asyncio
    assert isinstance(await asyncio.to_thread(ask, "devices"), list)
    monkeypatch.setattr(door.os, "getuid", lambda: -1)
    assert "own user" in (await asyncio.to_thread(ask, "devices"))["error"]


async def test_a_node_pulls_only_its_own_orders(served, env):
    ap = env[0]
    ap.issue_order("spawn", "mizar", {"title": "x"})
    with pytest.raises(Refused, match="only its own orders"):
        await locked(served[1], node="deneb").call("order_pull", {"node": "mizar"})
    assert await locked(served[1], node="deneb").call("order_pull", {}) == []
    assert len(await locked(served[1], node="mizar").call("order_pull", {})) == 1
