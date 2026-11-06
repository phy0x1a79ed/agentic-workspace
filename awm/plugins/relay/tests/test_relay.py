"""The relay's rooms, members, leases and feed, over a stand-in for the tether relay."""

from __future__ import annotations

import asyncio
import time

import pytest

from awm.relay import hub_adapter
from awm.relay.rooms import RoomGone
from awm.relay.service import Relay, RelayRefused
from awm.relay.store import Store

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

A = {"node": "alcor", "session": "cc" + "a" * 32, "name": "teacher", "level": "trusted",
     "who": "teacher@alcor"}
B = {"node": "mizar", "session": "cc" + "b" * 32, "name": "helper", "level": "cooperative",
     "who": "helper@mizar"}


class FakeRooms:
    """The tether relay's room routes: one room, a guest queue, and what we sent."""

    base = "http://127.0.0.1:9"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.seq = 0
        self.left: list[str] = []

    def link(self, name: str) -> str:
        return f"https://edge.test/room/{name}"

    async def open(self, lasts: str) -> tuple[str, str]:
        return ("acre-anchor-kettle" if lasts == "task" else "a-b-c-d-e-f"), "host-token"

    async def send(self, name: str, token: str, message: dict) -> int:
        self.seq += 1
        self.sent.append(message)
        return self.seq

    async def recv(self, name: str, token: str, after: int, wait: int = 25) -> dict:
        message = await self.inbox.get()
        if message == "gone":
            raise RoomGone(name)
        self.seq += 1
        if "ended" in message:
            return {"messages": [], "next": self.seq, "ended": message["ended"]}
        return {"messages": [{"seq": self.seq, "by": "guest", **message}], "next": self.seq}

    async def leave(self, name: str, token: str) -> None:
        self.left.append(name)

    async def aclose(self) -> None:
        pass


@pytest.fixture
async def relay(tmp_path):
    r = Relay(Store(tmp_path / "channels.db"), FakeRooms(), lease_s=0.3)
    yield r
    await r.stop()


async def _feed(relay, node="alcor", live=None, acks=(), wait=1.0):
    return await relay.feed(node, live, list(acks), wait)


async def test_a_guest_line_waits_for_the_member_and_an_ack_moves_it_on(relay):
    room = await relay.open(A, persistent=True)
    assert room["lasts"] == "persistent" and room["link"].endswith(room["name"])
    await relay.rooms.inbox.put({"from": "visitor", "text": "hello"})
    got = await _feed(relay, live=[A["session"]])
    [m] = got["messages"]
    assert (m["by"], m["who"], m["body"]) == ("guest", "visitor", {"text": "hello"})
    again = await _feed(relay, live=[A["session"]], wait=0)
    assert [x["id"] for x in again["messages"]] == [m["id"]], "unacknowledged comes again"
    ack = {"room": room["name"], "session": A["session"], "through": m["id"]}
    assert (await _feed(relay, live=[A["session"]], acks=[ack], wait=0))["messages"] == []


async def test_members_get_each_others_lines_and_never_their_own(relay):
    room = await relay.open(A)
    await relay.join(B, room["name"])
    await relay.write(A, room["name"], {"text": "from a"})
    assert relay.rooms.sent[-1] == {"from": "teacher@alcor", "text": "from a"}
    assert (await _feed(relay, "alcor", wait=0))["messages"] == []
    [m] = (await _feed(relay, "mizar", wait=0))["messages"]
    assert (m["by"], m["who"], m["level"]) == ("member", "teacher@alcor", "trusted")


async def test_an_encrypted_room_keeps_one_member_and_no_plaintext(relay):
    room = await relay.open(A)
    await relay.join(B, room["name"])
    with pytest.raises(RelayRefused, match="more than one member"):
        await relay.write(A, room["name"], {"spake2": "x"})
    await relay.close(B, room["name"])
    await relay.write(A, room["name"], {"spake2": "x"})
    with pytest.raises(RelayRefused, match="encrypted"):
        await relay.join(B, room["name"])
    await relay.write(A, room["name"], {"text": "still plain until it is sealed"})
    await relay.write(A, room["name"], {"sealed": "c2VhbGVk", "n": 0})
    with pytest.raises(RelayRefused, match="plaintext is refused"):
        await relay.write(A, room["name"], {"text": "a slip"})


async def test_a_lapsed_member_is_dropped_and_its_room_ends(relay):
    room = await relay.open(A, persistent=True)
    await _feed(relay, live=[A["session"]], wait=0)
    assert await relay.prune() == []
    time.sleep(0.4)
    await _feed(relay, live=[], wait=0)
    assert await relay.prune() == [f"{A['session']}@alcor"]
    assert relay.rooms.left == [room["name"]]
    assert relay.status(A)["rooms"][0]["ended"] == "its last member's session ended"


async def test_a_node_that_cannot_tell_renews_all_its_members(relay):
    await relay.open(A)
    time.sleep(0.4)
    await _feed(relay, live=None, wait=0)
    assert await relay.prune() == []


async def test_a_room_the_guest_ended_tells_the_member_once(relay):
    room = await relay.open(A)
    await relay.rooms.inbox.put({"ended": "the guest left"})
    [m] = (await _feed(relay, live=[A["session"]]))["messages"]
    assert (m["by"], m["body"]) == ("notice", {"text": "the guest left", "ended": True})
    ack = {"room": room["name"], "session": A["session"], "through": m["id"]}
    assert (await _feed(relay, live=[A["session"]], acks=[ack], wait=0))["members"] == []


async def test_only_a_member_writes_into_a_room(relay):
    room = await relay.open(A)
    with pytest.raises(RelayRefused, match="not a member"):
        await relay.write(B, room["name"], {"text": "x"})
    with pytest.raises(RelayRefused, match="carries"):
        await relay.write(A, room["name"], {"other": "x"})


async def test_the_adapter_takes_the_node_from_the_door_and_refuses_a_session(relay):
    hub_adapter.RELAY = relay
    member = {"session": A["session"], "name": "teacher", "level": "trusted"}
    opened = await hub_adapter.open_({"member": member, "_caller_trust": "trusted"}, "peer:alcor")
    assert relay.store.member(opened["name"], "alcor", A["session"])
    with pytest.raises(RelayRefused, match="its own node"):
        await hub_adapter.open_({"member": member, "_caller_trust": "trusted",
                                 "_caller_session": {"session_id": "cc1"}}, "peer:alcor")
    with pytest.raises(RelayRefused, match="channel service"):
        await hub_adapter.feed({"_caller_trust": "untrusted"}, "peer:alcor")
