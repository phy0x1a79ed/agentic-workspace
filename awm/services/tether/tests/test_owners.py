"""A session answers to the agent session that minted it, and to no other.

One daemon serves every agent on a node. Two agents helping the same owner are
still doing different work, so neither may read the other's commands, output or
conversation, or drive the other's session, by default. The slot cannot carry
that on its own because the relay hands slots out again.
"""

from __future__ import annotations

import pytest

from awm.tether import callers, hub_adapter, paths, stream

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

EPOCH = 0x5A5A1234

ALICE = {"_caller_session": {"job": "a11ce"}}
BOB = {"_caller_session": {"job": "b0b"}}
EDGE = "peer:capella"


def ev(seq: int, slot: int | None, type_: str = "owner.said", **fields) -> dict:
    e = {"type": type_, "seq": seq, "at": 1, **fields}
    if slot is not None:
        e["slot"] = slot
    return e


class FakeDaemon:
    """The daemon's control socket: a status table and a record of what it ran."""

    def __init__(self) -> None:
        self.live: dict[int, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.next_slot = 1

    async def call(self, verb, args, timeout=None):
        self.calls.append((verb, dict(args)))
        if verb == "status":
            return {"ok": True, "sessions": [
                {"slot": s, "code": code, "phase": "open"} for s, code in self.live.items()]}
        if verb == "invite":
            slot = self.next_slot
            self.next_slot += 1
            code = f"w{slot} secret words"
            self.live[slot] = code
            # The mint lands on the stream before the reply, as the daemon does it.
            feed(ev(hub_adapter.JOURNAL.head()["next"], slot, "session.minted"))
            return {"ok": True, "slot": slot, "code": code, "command": "curl -fsSL https://relay.test/tether | sh -s " + code}
        return {"ok": True, "verb": verb, "code": args.get("code")}


def feed(event: dict) -> None:
    hub_adapter.MARK.stamp(EPOCH, event)
    hub_adapter.JOURNAL.append(event)


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)
    monkeypatch.setattr(hub_adapter, "MINT_WAIT_S", 0.05)
    monkeypatch.setattr(callers, "_node_of", lambda as_: "capella")
    fake = FakeDaemon()
    monkeypatch.setattr(hub_adapter.control, "call", fake.call)
    hub_adapter.JOURNAL.reset(EPOCH)
    hub_adapter.MARK.begin(EPOCH)
    yield fake
    hub_adapter.JOURNAL.reset(0)


async def call(verb: str, who: dict, **args) -> dict:
    return await hub_adapter.HANDLERS[verb]({**args, **who}, EDGE)


async def test_drain_shows_a_caller_its_own_session_and_not_its_neighbours(daemon):
    a = await call("invite", ALICE, os="windows")
    b = await call("invite", BOB, os="windows")
    feed(ev(10, a["slot"], text="alice's owner"))
    feed(ev(11, b["slot"], "task.output", task=1, stream="stdout", data="Ym9i"))
    feed(ev(12, None, "daemon.started"))

    seen = await call("drain", ALICE)

    slots = {e.get("slot") for e in seen["events"]}
    assert b["slot"] not in slots
    assert a["slot"] in slots
    assert any(e["type"] == "daemon.started" for e in seen["events"]), \
        "a fact about no session is everyone's"


async def test_each_caller_keeps_its_own_cursor(daemon):
    """One shared cursor let a bare drain consume what the other was waiting for."""
    a = await call("invite", ALICE, os="unix")
    await call("drain", ALICE)
    await call("drain", BOB)
    feed(ev(20, a["slot"], text="hello"))

    await call("drain", BOB)
    again = await call("drain", ALICE)

    assert [e.get("text") for e in again["events"]] == ["hello"]


async def test_a_reused_slot_does_not_hand_the_old_session_to_the_new_owner(daemon):
    a = await call("invite", ALICE, os="unix")
    feed(ev(30, a["slot"], text="old"))
    feed(ev(31, a["slot"], "session.phase", phase="ended"))
    daemon.live.clear()
    daemon.next_slot = a["slot"]
    b = await call("invite", BOB, os="unix")
    feed(ev(40, b["slot"], text="new"))

    assert b["slot"] == a["slot"] and b["session"] != a["session"]
    bob = [e.get("text") for e in (await call("drain", BOB))["events"]]
    alice = [e.get("text") for e in (await call("drain", ALICE))["events"]]
    assert "old" not in bob and "new" in bob
    assert "new" not in alice and "old" in alice


async def test_the_listen_command_names_its_own_session(daemon):
    a = await call("invite", ALICE, os="unix")

    assert a["session"]
    assert a["listen"]["command"].endswith(f"--where session={a['session']}")
    assert "task." not in a["listen"]["command"]


async def test_a_verb_without_code_goes_to_the_callers_only_session(daemon):
    await call("invite", ALICE, os="unix")
    b = await call("invite", BOB, os="unix")

    answer = await call("send", BOB, text="hi")

    assert answer["ok"] is True
    assert daemon.calls[-1] == ("send", {"text": "hi", "code": str(b["slot"])})


async def test_two_of_your_own_sessions_and_no_code_is_refused(daemon):
    await call("invite", ALICE, os="unix")
    await call("invite", ALICE, os="unix")

    answer = await call("run", ALICE, command="whoami")

    assert answer["ok"] is False and "name one with `code`" in answer["error"]
    assert not any(verb == "run" for verb, _ in daemon.calls)


async def test_naming_another_operators_session_is_refused(daemon):
    a = await call("invite", ALICE, os="unix")

    for verb, extra in (("run", {"command": "whoami"}), ("send", {"text": "x"}),
                        ("cut", {}), ("tasks", {}), ("keys", {"task": 1, "text": "y"}),
                        ("close", {"task": 1}), ("drain", {})):
        answer = await call(verb, BOB, code=str(a["slot"]), **extra)
        assert answer["ok"] is False, verb
        assert "another operator" in answer["error"], verb
    assert not any(verb in ("run", "send", "cut", "tasks", "keys", "close")
                   for verb, _ in daemon.calls)


async def test_a_session_minted_before_owners_is_taken_by_whoever_names_it(daemon):
    daemon.live[1] = "w1 old words"
    feed(ev(1, 1, "session.minted"))
    feed(ev(2, 1, text="before the fix"))

    bare = await call("run", ALICE, command="whoami")
    assert bare["ok"] is False and "name it with `code`" in bare["error"]

    named = await call("drain", ALICE, code="1")
    assert [e.get("text") for e in named["events"] if e["type"] == "owner.said"] == \
        ["before the fix"]
    assert (await call("run", ALICE, command="whoami"))["ok"] is True
    assert (await call("run", BOB, code="1", command="whoami"))["ok"] is False


async def test_adopt_takes_a_session_back_after_its_operator_restarted(daemon):
    a = await call("invite", ALICE, os="unix")

    answer = await call("send", BOB, code=str(a["slot"]), text="back", adopt=True)

    assert answer["ok"] is True
    assert "adopt" not in daemon.calls[-1][1]
    assert (await call("send", ALICE, text="x"))["ok"] is False


async def test_all_lets_the_owner_of_the_node_read_everything(daemon):
    a = await call("invite", ALICE, os="unix")
    b = await call("invite", BOB, os="unix")
    feed(ev(50, a["slot"], text="a"))
    feed(ev(51, b["slot"], text="b"))

    every = await hub_adapter.drain({"all": True})

    assert {"a", "b"} <= {e.get("text") for e in every["events"]}


async def test_status_says_which_sessions_are_yours_and_hides_the_rest_of_the_code(daemon):
    a = await call("invite", ALICE, os="unix")
    b = await call("invite", BOB, os="unix")

    report = await call("status", ALICE)

    by_slot = {s["slot"]: s for s in report["sessions"]}
    assert by_slot[a["slot"]]["mine"] is True
    assert by_slot[a["slot"]]["code"] == a["code"]
    assert by_slot[b["slot"]]["mine"] is False
    assert by_slot[b["slot"]]["code"] == b["code"].split()[0]
    assert "b0b" in by_slot[b["slot"]]["operator"]


async def test_the_daemon_never_sees_who_called(daemon):
    await call("invite", ALICE, os="unix")
    await call("send", ALICE, text="hi")

    assert all("_caller_session" not in args and "_caller_pid" not in args
               for _, args in daemon.calls)


def test_a_local_session_is_known_by_its_job_and_a_terminal_by_nothing(monkeypatch):
    monkeypatch.setattr(callers, "_record", lambda pid: {
        "kind": "bg", "jobId": "9f862480", "name": "oct22 presentation"})
    assert callers.of({"_caller_pid": 550415}, None).id == "job:9f862480"
    monkeypatch.setattr(callers, "_record", lambda pid: {
        "kind": "interactive", "procStart": "77"})
    assert callers.of({"_caller_pid": 12}, None).id == "pid:12:77"
    assert callers.of({}, None) is callers.TERMINAL


def test_the_mint_an_event_belongs_to_survives_a_restart(tmp_path):
    mark = stream.Mark(tmp_path / "m.json")
    mark.begin(EPOCH)
    first = ev(5, 3, "session.minted")
    mark.stamp(EPOCH, first)
    mark.claim(3, "job:a11ce", "alice", floor=5)

    again = stream.Mark(tmp_path / "m.json")
    later = ev(9, 3)
    again.stamp(EPOCH, later)

    assert later["session"] == first["session"]
    assert again.owners[first["session"]]["by"] == "job:a11ce"


def test_a_claim_made_before_its_mint_arrives_settles_when_it_does(tmp_path):
    mark = stream.Mark(tmp_path / "m.json")
    mark.begin(EPOCH)
    mark.stamp(EPOCH, ev(1, 3, "session.minted"))

    assert mark.claim(3, "job:b0b", "bob", floor=4) is None, \
        "the old mint on the slot is not the one this invite made"
    minted = ev(4, 3, "session.minted")
    mark.stamp(EPOCH, minted)

    assert mark.owners[minted["session"]]["by"] == "job:b0b"
    assert mark.claims == {}
