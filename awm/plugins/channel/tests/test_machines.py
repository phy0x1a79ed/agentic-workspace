"""A machine channel against a stand-in for the tether operator daemon.

No tether session is ever opened. The stand-in answers the daemon's control
verbs and puts its events on the plugin's own stream journal, stamped as the
watch connection stamps them, so the tests show what the channel asks of the
daemon and what it does with what comes back.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from awm.channel import hub_adapter
from awm.channel.holders import ChannelError
from awm.gatewayclient.push import Pusher
from awm.channel.machines import Machines
from awm.channel.relay import Rooms
from awm.channel.tether import paths, stream

from .conftest import HOLDER, OTHER, eventually

PHRASE = "acre anchor kettle"
EPOCH = 5


class FakeDaemon:
    """The operator daemon: control verbs in, events out on the journal."""

    def __init__(self, machines: Machines) -> None:
        self.m = machines
        self.calls: list[tuple[str, dict]] = []
        self.live: set[int] = set()
        self.tasks: dict[int, list[dict]] = {}
        self.owner_os: str | None = None
        self.epoch = EPOCH
        machines.journal.reset(EPOCH)
        machines.journal.connected(True)
        machines.mark.begin(EPOCH)

    async def call(self, verb: str, args: dict, *, timeout: float = 30.0) -> dict:
        self.calls.append((verb, dict(args)))
        if verb == "invite":
            slot = 7
            self.live.add(slot)
            self.event(slot, "session.minted")
            return {"ok": True, "slot": slot, "code": f"seven {PHRASE}",
                    "command": f"curl -fsSL https://relay.test/tether | sh -s seven {PHRASE}"}
        if verb == "status":
            return {"ok": True, "sessions": [
                {"slot": s, "phase": "open",
                 "owner": {"os": self.owner_os} if self.owner_os else None}
                for s in self.live]}
        slot = int(args["code"])
        if slot not in self.live:
            return {"ok": False, "error": f"there is no session {slot} on this host"}
        if verb == "send":
            self.event(slot, "operator.said", text=args["text"])
            return {"ok": True}
        if verb == "run":
            task = len(self.tasks.setdefault(slot, [])) + 1
            self.tasks[slot].append({"task": task, "kind": "command"})
            return {"ok": True, "task": task}
        if verb == "shell":
            task = len(self.tasks.setdefault(slot, [])) + 1
            self.tasks[slot].append({"task": task, "kind": "shell"})
            return {"ok": True, "task": task}
        if verb == "tasks":
            return {"ok": True, "tasks": self.tasks.get(slot, [])}
        if verb in ("keys", "close"):
            return {"ok": True}
        if verb == "cut":
            self.event(slot, "session.phase", phase="ended", ended=args["reason"])
            self.live.discard(slot)
            return {"ok": True}
        raise AssertionError(verb)

    def event(self, slot: int, type_: str, **fields) -> None:
        ev = {"type": type_, "seq": self.m.journal.head()["next"], "slot": slot, **fields}
        self.m.mark.stamp(self.epoch, ev)
        self.m.journal.append(ev)

    def output(self, slot: int, task: int, text: str) -> None:
        self.event(slot, "task.output", task=task, stream="stdout",
                   data=base64.b64encode(text.encode()).decode(), bytes=len(text))


@pytest.fixture
def daemon(machines, monkeypatch) -> FakeDaemon:
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)
    fake = FakeDaemon(machines)
    machines._call = fake.call
    return fake


def _build(tmp_path, machines, pushed, live, sessions):
    async def list_jobs(node):
        return live.get(node)

    return hub_adapter.build(tmp_path / "state",
                             rooms=Rooms("http://127.0.0.1:9", "unused"),
                             pusher=Pusher(send=pushed.send), list_jobs=list_jobs,
                             list_sessions=sessions, machines=machines)


@pytest.fixture
async def chans(tmp_path, machines, daemon, pushed, live, sessions, monkeypatch):
    from awm.channel import holders

    monkeypatch.setattr(holders.config, "node_name", lambda: "testnode")
    built = _build(tmp_path, machines, pushed, live, sessions)
    yield built
    await asyncio.wait_for(built.stop(), 5)
    await built.rooms.aclose()


def _sent(daemon, verb):
    return [args for v, args in daemon.calls if v == verb]


async def test_open_mints_a_session_and_names_the_channel_by_it(chans, daemon, tmp_path):
    opened = await chans.open(HOLDER, "machine", "windows")
    assert opened["kind"] == "machine" and opened["trust"] == "untrusted"
    assert opened["name"] == chans.machines.mark.current(7)
    assert opened["code"] == f"seven {PHRASE}"
    assert opened["read_out"].startswith("Open PowerShell")
    assert "[scriptblock]" in opened["command"]
    assert opened["os"]["declared"] == "windows"
    assert daemon.calls[0] == ("invite", {})
    owner = chans.machines.mark.owners[opened["name"]]
    assert owner["by"] == f"testnode/{HOLDER['session']}"
    # The phrase goes to the holder once and is never kept.
    db = (tmp_path / "state" / "channels.db").read_bytes()
    assert PHRASE.encode() not in db


@pytest.mark.parametrize("given", [None, "Windows", "wsl"])
async def test_a_machine_needs_a_declared_os(chans, daemon, given):
    with pytest.raises(ChannelError, match="os"):
        await chans.open(HOLDER, "machine", given)
    assert daemon.calls == [], "a session was minted on a guess"


async def test_what_the_owner_types_is_pushed_and_command_output_is_not(
        chans, daemon, pushed):
    opened = await chans.open(HOLDER, "machine", "unix")
    name = opened["name"]
    daemon.event(7, "session.phase", phase="open")
    daemon.event(7, "owner.said", text="the printer says offline")
    ran = await chans.write(HOLDER, "lpstat -p", target="shell")
    daemon.output(7, ran["task"], "printer-noise is idle")
    await chans.write(HOLDER, "is it plugged in?")
    daemon.event(7, "owner.said", text="yes")
    texts = await eventually(lambda: pushed.texts() if len(pushed.texts()) == 3 else None)
    assert texts == [
        f"machine {name}: the owner connected.",
        f"machine {name} [untrusted] from the owner: the printer says offline",
        f"machine {name} [untrusted] from the owner: yes",
    ]
    await asyncio.sleep(0.05)
    assert len(pushed.texts()) == 3
    transcript = Path(opened["transcript"]).read_text()
    assert "is it plugged in?" in transcript
    assert "printer-noise" not in transcript and "lpstat" not in transcript


async def test_say_reaches_the_person_and_shell_runs_a_command(chans, daemon):
    await chans.open(HOLDER, "machine", "unix")
    assert (await chans.write(HOLDER, "hello"))["sealed"] is True
    assert _sent(daemon, "send") == [{"code": "7", "text": "hello"}]
    ran = await chans.write(HOLDER, "uname -a", target="shell", limit_s=30)
    assert ran["task"] == 1 and ran["os"]["shell"] == "POSIX sh"
    assert _sent(daemon, "run") == [{"code": "7", "command": "uname -a", "limit_s": 30}]


async def test_drain_reads_the_tool_log_once(chans, daemon):
    await chans.open(HOLDER, "machine", "unix")
    ran = await chans.write(HOLDER, "echo hi", target="shell")
    daemon.output(7, ran["task"], "h")
    daemon.output(7, ran["task"], "i\n")
    daemon.event(7, "owner.said", text="not a log line")
    daemon.event(7, "task.exited", task=ran["task"], code=0)
    log = await chans.drain(HOLDER)
    assert [e["type"] for e in log["events"]] == ["task.output", "task.exited"]
    assert log["events"][0]["text"] == "hi\n"
    assert (await chans.drain(HOLDER))["events"] == []


async def test_drain_until_waits_for_the_task_to_end(chans, daemon):
    await chans.open(HOLDER, "machine", "unix")
    ran = await chans.write(HOLDER, "sleep 1", target="shell")
    waiting = asyncio.create_task(chans.drain(HOLDER, wait=5, until=ran["task"]))
    await asyncio.sleep(0.05)
    daemon.event(7, "task.exited", task=ran["task"], code=0)
    log = await asyncio.wait_for(waiting, 3)
    assert log["events"][-1]["type"] == "task.exited"


async def test_keys_type_raw_into_the_newest_task(chans, daemon):
    await chans.open(HOLDER, "machine", "unix")
    with pytest.raises(ChannelError, match="no task is open"):
        await chans.write(HOLDER, "\x03", target="shell", keys=True)
    await chans.write(HOLDER, "", target="shell", terminal=True, cols=100, rows=30)
    assert _sent(daemon, "shell") == [{"code": "7", "cols": 100, "rows": 30}]
    typed = await chans.write(HOLDER, "\x03", target="shell", keys=True)
    assert typed["task"] == 1
    assert _sent(daemon, "keys") == [{"code": "7", "task": 1, "data": "Aw=="}]


async def test_close_with_a_task_stops_it_and_keeps_the_channel(chans, daemon):
    await chans.open(HOLDER, "machine", "unix")
    await chans.write(HOLDER, "top", target="shell")
    assert (await chans.close(HOLDER, task=1))["stopped"] == 1
    assert _sent(daemon, "close") == [{"code": "7", "task": 1}]
    assert (await chans.status(HOLDER))["channels"][0]["open"]


async def test_close_cuts_the_session_and_pushes_nothing(chans, daemon, pushed):
    opened = await chans.open(HOLDER, "machine", "unix")
    await chans.close(HOLDER)
    assert _sent(daemon, "cut")[0]["code"] == "7"
    assert 7 not in daemon.live
    status = (await chans.status(HOLDER))["channels"][0]
    assert status["name"] == opened["name"] and not status["open"]
    await asyncio.sleep(0.05)
    assert pushed.texts() == []


async def test_the_owner_leaving_ends_the_channel(chans, daemon, pushed):
    opened = await chans.open(HOLDER, "machine", "unix")
    daemon.event(7, "session.phase", phase="ended", ended="the owner left")
    line = await eventually(lambda: pushed.texts())
    assert line == [f"machine {opened['name']}: ended, the owner left. "
                    "It is closed on our side."]
    assert (await chans.status(HOLDER))["channels"][0]["ended"] == "the owner left"


async def test_a_holder_whose_session_ended_has_its_machine_cut(chans, daemon, sessions):
    opened = await chans.open(HOLDER, "machine", "unix")
    sessions.ids = {OTHER["session"]}
    assert await chans.prune() == [opened["name"]]
    assert 7 not in daemon.live


async def test_only_the_holder_reaches_the_session(chans, daemon):
    opened = await chans.open(HOLDER, "machine", "unix")
    for verb in (chans.write(OTHER, "ls", opened["name"], target="shell"),
                 chans.drain(OTHER, opened["name"]),
                 chans.close(OTHER, opened["name"])):
        with pytest.raises(ChannelError, match="no open channel"):
            await verb
    assert _sent(daemon, "run") == [] and _sent(daemon, "cut") == []


async def test_a_reused_slot_is_not_the_channels_session(chans, daemon):
    await chans.open(HOLDER, "machine", "unix")
    daemon.event(7, "session.phase", phase="ended", ended="gone")
    daemon.event(7, "session.minted")
    with pytest.raises(ChannelError):
        await chans.write(HOLDER, "ls", target="shell")
    assert _sent(daemon, "run") == []


async def test_a_new_daemon_epoch_ends_the_channel_and_resets_the_drain(
        chans, daemon, pushed):
    opened = await chans.open(HOLDER, "machine", "unix")
    daemon.epoch = EPOCH + 1
    chans.machines.journal.reset(daemon.epoch)
    chans.machines.mark.begin(daemon.epoch)
    daemon.event(7, "session.minted")
    line = await eventually(lambda: pushed.texts())
    assert "the tether daemon restarted" in line[0]
    [row] = (await chans.status(HOLDER))["channels"]
    assert row["name"] == opened["name"] and not row["open"]


async def test_drain_says_when_the_daemon_restarted_under_it(chans, daemon):
    await chans.open(HOLDER, "machine", "unix")
    await chans.stop()  # no follow loop to close the channel first
    daemon.epoch = EPOCH + 1
    chans.machines.journal.reset(daemon.epoch)
    log = await chans.drain(HOLDER)
    assert log["events"] == [] and "restarted" in log["reset"]


async def test_a_restart_resumes_without_pushing_anything_twice(
        tmp_path, machines, daemon, pushed, live, sessions, chans):
    await chans.open(HOLDER, "machine", "unix")
    daemon.event(7, "owner.said", text="before")
    await eventually(lambda: pushed.texts())
    await chans.pusher.idle()
    await chans.stop()
    daemon.event(7, "owner.said", text="during")

    again = _build(tmp_path, machines, pushed, live, sessions)
    again.resume()
    await eventually(lambda: len(pushed.texts()) == 2)
    await again.pusher.idle()
    assert [t.rsplit(": ", 1)[1] for t in pushed.texts()] == ["before", "during"]
    await again.stop()
    await again.rooms.aclose()


async def test_the_relay_host_mints_no_machine(chans, daemon, monkeypatch):
    monkeypatch.setattr(paths, "ROLE", paths.RELAY)
    with pytest.raises(ChannelError, match="this host is the relay"):
        await chans.open(HOLDER, "machine", "unix")


async def test_status_of_a_machine_names_its_tasks_and_flags_an_os_mismatch(chans, daemon):
    opened = await chans.open(HOLDER, "machine", "unix")
    await chans.write(HOLDER, "ls", target="shell")
    daemon.owner_os = "windows"
    [row] = (await chans.status(HOLDER, opened["name"]))["channels"]
    assert row["tasks"] == [{"task": 1, "kind": "command"}]
    assert row["os"]["mismatch"] is True and "ask the owner" in row["os"]["warning"]
    assert row["tether"]["role"] == paths.OPERATOR


def test_the_mark_keeps_who_minted_each_session(tmp_path):
    mark = stream.Mark(tmp_path / "m.json")
    mark.begin(1)
    assert mark.claim(3, "n/s", "label", 0) is None
    ev = {"type": "session.minted", "seq": 0, "slot": 3}
    mark.stamp(1, ev)
    assert mark.owners[ev["session"]]["by"] == "n/s"
