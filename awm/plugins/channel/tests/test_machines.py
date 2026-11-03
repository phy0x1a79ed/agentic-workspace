"""A machine channel against a stand-in for the tether tool.

No tether session is ever opened: the stand-in answers ``invite``, ``send``,
``cut`` and ``drain`` the way the tether's gateway adapter does, including its
ownership rule, so the tests show what the channel asks of it and in whose name.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from awm.channel import hub_adapter
from awm.channel.holders import ChannelError, Pusher
from awm.channel.machines import Machines
from awm.channel.relay import Rooms

from .conftest import HOLDER, eventually

LOCAL = {**HOLDER, "pid": 4242}
PHRASE = "acre anchor kettle"


class FakeTether:
    """The tether tool as the gateway serves it: one journal, sessions by slot."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, int | None]] = []
        self.events: list[dict] = []
        self.owner: dict[int, str | None] = {}
        self.live: set[int] = set()
        self._changed = asyncio.Condition()

    async def call(self, service: str, verb: str, args: dict, *,
                   caller_pid: int | None = None, timeout: float = 30.0) -> dict:
        assert service == "tether"
        self.calls.append((verb, dict(args), caller_pid))
        if verb == "invite":
            slot = 7
            self.owner[slot], self.live = (f"pid:{caller_pid}" if caller_pid else None,
                                           self.live | {slot})
            return {"ok": True, "slot": slot, "code": f"seven {PHRASE}",
                    "session": f"{slot}-1-abcd", "command": "irm ... | iex",
                    "read_out": "Open PowerShell, then ...", "os": {"declared": args["os"]}}
        slot = int(args["code"])
        if slot not in self.live:
            return {"ok": False, "error": f"`{slot}` names no session: give its slot number"}
        mine = caller_pid is not None and self.owner[slot] == f"pid:{caller_pid}"
        if not (mine or args.get("adopt")):
            return {"ok": False, "error": f"session {slot} belongs to another operator"}
        if verb == "send":
            await self.say(slot, "operator.said", text=args["text"])
            return {"ok": True}
        if verb == "cut":
            await self.say(slot, "session.phase", phase="ended", ended=args["reason"])
            self.live.discard(slot)
            return {"ok": True}
        if verb == "drain":
            return await self._drain(slot, int(args["cursor"]), float(args["wait"]),
                                     tuple(args["types"].split(",")))
        raise AssertionError(verb)

    async def say(self, slot: int, type_: str, **fields) -> None:
        async with self._changed:
            self.events.append({"type": type_, "seq": len(self.events), "slot": slot,
                                **fields})
            self._changed.notify_all()

    async def _drain(self, slot, cursor, wait, types):
        def found():
            return [e for e in self.events[cursor:]
                    if e["slot"] == slot and e["type"].startswith(types)]
        async with self._changed:
            if not found():
                try:
                    await asyncio.wait_for(self._changed.wait(), wait)
                except asyncio.TimeoutError:
                    pass
        return {"ok": True, "cursor": len(self.events), "events": found()}


@pytest.fixture
def tether() -> FakeTether:
    return FakeTether()


def _build(tmp_path, tether, pushed, live):
    async def list_jobs(node):
        return live.get(node)

    return hub_adapter.build(tmp_path / "state",
                             rooms=Rooms("http://127.0.0.1:9", "unused"),
                             pusher=Pusher(send=pushed.send), list_jobs=list_jobs,
                             machines=Machines(call=tether.call))


@pytest.fixture
async def machines(tmp_path, tether, pushed, live):
    built = _build(tmp_path, tether, pushed, live)
    yield built
    await asyncio.wait_for(built.stop(), 5)
    await built.rooms.aclose()


async def test_open_mints_a_session_in_the_holders_name(machines, tether, tmp_path):
    opened = await machines.open(LOCAL, "machine", "windows")
    assert opened["kind"] == "machine" and opened["trust"] == "untrusted"
    assert opened["code"] == f"seven {PHRASE}"
    assert opened["read_out"].startswith("Open PowerShell")
    assert tether.calls[0] == ("invite", {"os": "windows"}, 4242)
    # The phrase goes to the holder once and is never kept.
    db = (tmp_path / "state" / "channels.db").read_bytes()
    assert PHRASE.encode() not in db


async def test_a_machine_needs_its_os(machines, tether):
    with pytest.raises(ChannelError, match="`os`"):
        await machines.open(LOCAL, "machine")
    assert tether.calls == []


async def test_what_the_owner_types_is_pushed_and_our_echo_is_not(machines, tether, pushed):
    opened = await machines.open(LOCAL, "machine", "unix")
    name = opened["name"]
    await tether.say(7, "session.phase", phase="open", owner={"user": "pat"})
    await tether.say(7, "task.output", data="bm9pc2U=")
    await tether.say(7, "owner.said", text="the printer says offline")
    await machines.write(LOCAL, "is it plugged in?")
    await tether.say(7, "owner.said", text="yes")
    texts = await eventually(lambda: pushed.texts() if len(pushed.texts()) == 3 else None)
    assert texts == [
        f"machine {name}: the owner connected.",
        f"machine {name} [untrusted] from the owner: the printer says offline",
        f"machine {name} [untrusted] from the owner: yes",
    ]
    transcript = Path(opened["transcript"]).read_text()
    assert "is it plugged in?" in transcript and "noise" not in transcript


async def test_write_goes_to_the_holders_session_with_its_stamp(machines, tether):
    await machines.open(LOCAL, "machine", "unix")
    sent = await machines.write(LOCAL, "hello")
    assert sent["sealed"] is True
    assert ("send", {"code": "7", "text": "hello"}, 4242) in tether.calls


async def test_close_cuts_the_session(machines, tether, pushed):
    opened = await machines.open(LOCAL, "machine", "unix")
    await machines.close(LOCAL)
    cut = [c for c in tether.calls if c[0] == "cut"]
    assert cut and cut[0][1]["code"] == "7" and cut[0][1]["adopt"] is True
    assert 7 not in tether.live
    status = machines.status(LOCAL)["channels"][0]
    assert status["name"] == opened["name"] and not status["open"]
    await asyncio.sleep(0.05)
    assert pushed.texts() == []


async def test_the_owner_leaving_ends_the_channel(machines, tether, pushed):
    opened = await machines.open(LOCAL, "machine", "unix")
    await tether.say(7, "session.phase", phase="ended", ended="the owner left")
    line = await eventually(lambda: pushed.texts())
    assert line == [f"machine {opened['name']}: ended, the owner left. "
                    "It is closed on our side."]
    assert machines.status(LOCAL)["channels"][0]["ended"] == "the owner left"


async def test_a_holder_whose_job_ended_has_its_session_cut(machines, tether, live):
    await machines.open(LOCAL, "machine", "unix")
    live["testnode"] = set()
    assert await machines.prune() == ["7-1-abcd"]
    assert 7 not in tether.live


async def test_a_restart_resumes_without_pushing_anything_twice(
        tmp_path, tether, pushed, live, machines):
    await machines.open(LOCAL, "machine", "unix")
    await tether.say(7, "owner.said", text="before")
    await eventually(lambda: pushed.texts())
    await machines.pusher.idle()
    await machines.stop()
    await tether.say(7, "owner.said", text="during")

    again = _build(tmp_path, tether, pushed, live)
    again.resume()
    await eventually(lambda: len(pushed.texts()) == 2)
    await again.pusher.idle()
    assert [t.rsplit(": ", 1)[1] for t in pushed.texts()] == ["before", "during"]
    await again.stop()
    await again.rooms.aclose()


def test_the_tether_still_answers_the_way_the_channel_reads_it():
    """The phrases and params the channel keys off, in the tether's own adapter."""
    root = Path(__file__).resolve().parents[4]
    src = (root / "awm/services/tether/awm/tether/hub_adapter.py").read_text()
    assert "names no session" in src
    for param in ("adopt", "cursor", "types", "wait", "code"):
        assert re.search(rf'args\.get\("{param}"\)|"{param}"', src), param
    assert '"tether": None' in (root / "awm/gateway/awm/gateway/server.py").read_text()
