"""A room end to end: our side through the channel service, the other side as
an agent that has only the link, both over a real relay on loopback."""

from __future__ import annotations

import pytest

from awm.channel import hub_adapter, lines
from awm.channel.service import ChannelError
from awm.gatewayclient.push import Pusher
from awm.channel.relay import Rooms

from .conftest import BEARER, FAR, HOLDER, OTHER, eventually, needs_relay
from .guest import Guest

pytestmark = [pytest.mark.integration, needs_relay]


async def test_a_guest_with_only_the_link_talks_with_the_holder(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    assert opened["trust"] == "untrusted"
    guest = Guest(opened["link"])
    try:
        assert "ROOM/send" in await guest.how_to()
        await guest.say("hello from outside secret-body-marker")
        line = await eventually(lambda: pushed.texts())
        assert line == [f"room {opened['name']} [untrusted] from visitor: "
                        "hello from outside secret-body-marker"]

        await channels.write(HOLDER, "hello back")
        heard = await guest.hear()
        assert [m["text"] for m in heard["messages"]] == ["hello back"]
        assert heard["messages"][0]["from"] == "tester"
    finally:
        await guest.aclose()


async def test_our_own_writes_are_not_pushed_back_to_us(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await channels.write(HOLDER, "mine")
        await guest.say("theirs")
        await eventually(lambda: pushed.texts())
        await channels.pusher.idle()
        assert len(pushed.texts()) == 1 and pushed.texts()[0].endswith(": theirs")
        transcript = channels.transcripts.path(opened["name"]).read_text()
        assert "host tester" in transcript and "mine" in transcript
    finally:
        await guest.aclose()


async def test_a_long_message_is_cut_in_the_prompt_and_whole_in_the_transcript(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    long = "word " * 1000
    try:
        await guest.say(long)
        [line] = await eventually(lambda: pushed.texts())
        assert "…(cut at" in line and opened["transcript"] in line
        assert long in channels.transcripts.path(opened["name"]).read_text()
    finally:
        await guest.aclose()


async def test_the_guest_leaving_closes_the_channel_and_tells_the_holder(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await guest.leave()
        lines_ = await eventually(lambda: pushed.texts())
        assert lines_ == [f"room {opened['name']}: ended, the guest left. "
                          "It is closed on our side."]
        [row] = (await channels.status(HOLDER))["channels"]
        assert not row["open"] and row["ended"] == "the guest left"
    finally:
        await guest.aclose()


async def test_closing_ends_the_room_for_the_guest(channels):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        assert await channels.close(HOLDER) == {"closed": opened["name"]}
        heard = await guest.hear()
        assert heard["ended"] == "the host closed the room"
        with pytest.raises(ChannelError, match="no open channel"):
            await channels.write(HOLDER, "after the end")
    finally:
        await guest.aclose()


async def test_a_channel_closes_once_its_holding_session_ends(channels, sessions):
    opened = await channels.open(HOLDER, "room")
    kept = await channels.open(OTHER, "room")
    guest = Guest(opened["link"])
    try:
        sessions.ids = {OTHER["session"]}
        assert await channels.prune() == [opened["name"]]
        assert (await guest.hear())["ended"] == "the host closed the room"
        assert [r["open"] for r in (await channels.status(OTHER))["channels"]] == [True]
        assert kept["name"] != opened["name"]
    finally:
        await guest.aclose()


async def test_an_isolated_session_holds_a_room_by_its_session_id(channels, pushed, sessions):
    """It has no job: the push and the prune both go by its R9 id (R111)."""
    iso = "cc" + "abcdef0123456789" * 2
    holder = {"node": "testnode", "session": iso, "job": iso, "name": "boxed"}
    sessions.ids = {iso}
    opened = await channels.open(holder, "room")
    guest = Guest(opened["link"])
    try:
        await guest.say("to the container")
        [line] = await eventually(lambda: pushed.texts(iso))
        assert line.endswith(": to the container")
        assert await channels.prune() == []
        sessions.ids = set()
        assert await channels.prune() == [opened["name"]]
    finally:
        await guest.aclose()


async def test_a_session_that_cannot_be_checked_keeps_its_channels(channels, sessions):
    await channels.open(HOLDER, "room")
    sessions.ids = None
    assert await channels.prune() == []


async def test_a_holder_on_another_node_is_checked_by_its_job(channels, live):
    opened = await channels.open(FAR, "room")
    assert await channels.prune() == []
    live["farnode"] = set()
    assert await channels.prune() == [opened["name"]]


async def test_a_node_that_cannot_list_its_jobs_keeps_its_channels(channels, live):
    await channels.open(FAR, "room")
    live["farnode"] = None
    assert await channels.prune() == []


async def test_one_session_holds_a_task_room_and_a_persistent_room(channels):
    task = await channels.open(HOLDER, "room")
    kept = await channels.open(HOLDER, "room", persistent=True)
    assert task["lasts"] == "task" and len(task["name"].split("-")) == 3
    assert kept["lasts"] == "persistent" and len(kept["name"].split("-")) == 6
    assert kept["link"].endswith(f"/room/{kept['name']}")
    guest = Guest(kept["link"])
    try:
        assert "ROOM/recv" in await guest.how_to()
        # Closing one early leaves the other open.
        assert await channels.close(HOLDER, task["name"]) == {"closed": task["name"]}
        rows = {r["name"]: r for r in (await channels.status(HOLDER))["channels"]}
        assert not rows[task["name"]]["open"] and rows[kept["name"]]["open"]
        assert rows[kept["name"]]["lasts"] == "persistent"
        await channels.write(HOLDER, "still here", kept["name"])
        assert (await guest.hear())["messages"][0]["text"] == "still here"
    finally:
        await guest.aclose()


async def test_a_machine_channel_is_never_persistent(channels):
    with pytest.raises(ChannelError, match="only a room can be persistent"):
        await channels.open(HOLDER, "machine", "unix", persistent=True)


async def test_a_shell_write_and_a_drain_on_a_room_do_nothing(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        out = await channels.write(HOLDER, "rm -rf /", target="shell")
        assert out["sent"] is False and "does nothing" in out["note"]
        assert (await channels.drain(HOLDER))["events"] == []
        await channels.write(HOLDER, "only this")
        heard = await guest.hear()
        assert [m.get("text") for m in heard["messages"]] == ["only this"]
    finally:
        await guest.aclose()


async def test_a_push_the_holder_refuses_as_gone_closes_its_channels(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        pushed.gone.add(HOLDER["job"])
        await guest.say("anyone there?")
        assert (await guest.hear(wait=10))["ended"] == "the host closed the room"
    finally:
        await guest.aclose()


async def test_a_channel_is_only_its_holders(channels):
    opened = await channels.open(HOLDER, "room")
    with pytest.raises(ChannelError, match="no open channel"):
        await channels.write(OTHER, "not mine", opened["name"])
    with pytest.raises(ChannelError, match="no open channel"):
        await channels.close(OTHER, opened["name"])
    assert (await channels.status(OTHER)) == {"channels": []}


async def test_a_holder_of_two_channels_names_one(channels):
    a = await channels.open(HOLDER, "room")
    b = await channels.open(HOLDER, "room")
    with pytest.raises(ChannelError, match="more than one"):
        await channels.write(HOLDER, "which?")
    assert (await channels.write(HOLDER, "this one", b["name"]))["name"] == b["name"]
    assert a["name"] != b["name"]


async def test_a_restarted_service_resumes_without_pushing_anything_twice(
        tmp_path, relay, pushed, live, channels):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await guest.say("before the restart")
        await eventually(lambda: pushed.texts())
        await channels.pusher.idle()
        await channels.stop()

        await guest.say("during the restart")

        async def list_jobs(node):
            return live.get(node)

        again = hub_adapter.build(tmp_path / "state", rooms=Rooms(relay, BEARER),
                                  pusher=Pusher(send=pushed.send), list_jobs=list_jobs)
        again.resume()
        await eventually(lambda: len(pushed.texts()) == 2)
        await again.pusher.idle()
        assert [line.rsplit(": ", 1)[1] for line in pushed.texts()] == [
            "before the restart", "during the restart"]
        await again.stop()
        await again.rooms.aclose()
    finally:
        await guest.aclose()


def test_lines_cap_is_the_one_the_tool_promises():
    assert lines.TEXT_MAX == 1500


def test_the_plugin_does_not_reach_into_social():
    """Seats and channels share code only through gatewayclient (install.md R117 item 1)."""
    from pathlib import Path

    from awm.channel import service

    for py in Path(service.__file__).parent.rglob("*.py"):
        assert "awm.social" not in py.read_text(), py
