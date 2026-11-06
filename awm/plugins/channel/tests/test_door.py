"""Rooms held from member nodes through the public host's door (fleet.md R79, R98;
channels.md R72 item 5): the host never calls out, members pull, and a room and
its unread messages outlive a restart of the host's service."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from .conftest import (FAR, HOLDER, Host, Sessions, eventually, member_node, needs_relay,
                       stop_node)
from .guest import Guest

pytestmark = [pytest.mark.integration, needs_relay]


async def test_sessions_on_two_nodes_share_one_room_through_the_door(
        channels, pushed, tmp_path):
    opened = await channels.open(HOLDER, "room")
    far = member_node(tmp_path, "farnode", pushed, Sessions({FAR["session"]}))
    guest = Guest(opened["link"])
    try:
        joined = await far.join(FAR, opened["name"])
        assert joined["link"] == opened["link"]

        await channels.write(HOLDER, "from the first node")
        [line] = await eventually(lambda: pushed.texts(FAR["job"]))
        assert line == f"room {opened['name']} [trusted] from tester@testnode: from the first node"

        await far.write(FAR, "from the second node")
        await eventually(lambda: any(t.endswith("from far@farnode: from the second node")
                                     for t in pushed.texts()))

        await guest.say("to everyone")
        await eventually(lambda: any(t.endswith("from visitor: to everyone")
                                     for t in pushed.texts(FAR["job"])))
        await eventually(lambda: any(t.endswith("from visitor: to everyone")
                                     for t in pushed.texts()))
        heard = [m["text"] for m in (await guest.hear())["messages"]]
        assert "from the first node" in heard
        # No member gets its own lines back.
        assert not any("from the first node" in t for t in pushed.texts())
        assert not any("from the second node" in t for t in pushed.texts(FAR["job"]))
        assert set(channels.door.calls) | set(far.door.calls) <= {"open", "join", "write", "feed"}
    finally:
        await guest.aclose()
        await stop_node(far)


async def test_a_room_and_its_unread_messages_outlive_a_restart_of_the_host(
        tmp_path, relay, pushed, monkeypatch):
    """The cutover's shape: a holder on node A through the door, a guest over the
    link, and the host's service restarted while a message waits."""
    from awm.gatewayclient import holders

    monkeypatch.setattr(holders.config, "node_name", lambda: "testnode")
    host = Host(relay, tmp_path / "deneb")
    host.start()
    node = member_node(tmp_path, "testnode", pushed, Sessions({HOLDER["session"]}))
    opened = await node.open(HOLDER, "room", persistent=True)
    guest = Guest(opened["link"])
    try:
        await guest.say("before the restart")
        await eventually(lambda: pushed.texts())

        # The holder's node is away, and the host's service restarts while a line waits.
        await stop_node(node)
        await guest.say("while the holder was away")
        await eventually(lambda: any(b"while the holder was away" in p.read_bytes()
                                     for p in host.files() if p.name == "channels.db"))
        await host.stop()
        await guest.say("while the host restarted")
        host.start()

        node = member_node(tmp_path, "testnode", pushed, Sessions({HOLDER["session"]}))
        await eventually(lambda: len(pushed.texts()) == 3, timeout=10)
        await node.pusher.idle()
        assert [line.rsplit(": ", 1)[1] for line in pushed.texts()] == [
            "before the restart", "while the holder was away", "while the host restarted"]

        await node.write(HOLDER, "after it all")
        heard = [m["text"] for m in (await guest.hear())["messages"]]
        assert heard[-1] == "after it all"
        [row] = (await node.status(HOLDER))["channels"]
        assert row["open"] and row["lasts"] == "persistent"
    finally:
        await guest.aclose()
        await stop_node(node)
        await host.stop()


async def test_a_member_whose_session_ended_lapses_and_its_room_ends(
        tmp_path, relay, pushed, monkeypatch):
    from awm.gatewayclient import holders

    monkeypatch.setattr(holders.config, "node_name", lambda: "testnode")
    host = Host(relay, tmp_path / "deneb", lease_s=0.5)
    host.start()
    running = Sessions({HOLDER["session"]})
    node = member_node(tmp_path, "testnode", pushed, running, feed_wait_s=0.2)
    opened = await node.open(HOLDER, "room", persistent=True)
    guest = Guest(opened["link"])
    try:
        import asyncio

        await asyncio.sleep(1.0)
        assert await host.service.prune() == [], "a fed member keeps its lease"
        # The session ends. Its node stops renewing it and no longer fetches for it.
        running.ids = set()
        await asyncio.sleep(1.0)
        assert await host.service.prune() == [f"{HOLDER['session']}@testnode"]
        assert (await guest.hear())["ended"] == "the host closed the room"
    finally:
        await guest.aclose()
        await stop_node(node)
        await host.stop()


async def test_an_isolated_session_holds_a_room_through_the_door(channels, pushed, sessions):
    iso = "cc" + "abcdef0123456789" * 2
    holder = {"node": "testnode", "session": iso, "job": iso, "name": "boxed",
              "level": "untrusted", "isolated": True}
    sessions.ids = {iso}
    opened = await channels.open(holder, "room", persistent=True)
    guest = Guest(opened["link"])
    try:
        await guest.say("to the box")
        [line] = await eventually(lambda: pushed.texts(iso))
        assert line.endswith("from visitor: to the box")
        await channels.write(holder, "from the box")
        assert (await guest.hear())["messages"][-1]["from"] == "boxed@testnode"
    finally:
        await guest.aclose()


def test_the_host_opens_no_connection_of_its_own():
    """The relay plugin needs no tool, contract, door or edge, and calls no gateway."""
    root = Path(__file__).resolve().parents[2] / "relay"
    manifest = tomllib.loads((root / "awm-plugin.toml").read_text())
    needs = manifest.get("needs") or {}
    assert not needs.get("tool") and not needs.get("contract"), needs
    for py in (root / "awm").rglob("*.py"):
        text = py.read_text()
        for marker in ("gatewayclient.call", "call_sync", "reflection", "door", "Pusher"):
            if marker == "door" and "door" in text and py.name in ("service.py", "hub_adapter.py"):
                # Docstrings name the door the calls arrive through; no code dials it.
                assert "door.call" not in text and "ssh" not in text, py
                continue
            assert marker not in text, f"{py}: {marker}"


async def test_a_session_cannot_call_the_relay_itself(host):
    from awm.relay import hub_adapter as relay_adapter
    from awm.relay.service import RelayRefused

    member = {"session": HOLDER["session"], "name": "x", "level": "trusted"}
    with pytest.raises(RelayRefused, match="its own node"):
        await relay_adapter.open_({"member": member, "_caller_trust": "trusted",
                                   "_caller_session": {"session_id": HOLDER["session"]}},
                                  "peer:testnode")
    with pytest.raises(RelayRefused, match="channel service"):
        await relay_adapter.open_({"member": member, "_caller_trust": "cooperative"},
                                  "peer:testnode")
