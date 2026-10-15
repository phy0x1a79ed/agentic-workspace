"""The Discord commands the door consumes: /reject and /new-agent."""

from __future__ import annotations

import asyncio

import pytest

from awm.reporting.commands import Commands
from awm.reporting.federation import FedError

from stubs import FakeCx, FakeFed, make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class Replies:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.error: Exception | None = None

    async def __call__(self, account: str, channel: str, text: str) -> None:
        if self.error:
            raise self.error
        self.sent.append((account, channel, text))


def event(command, **over):
    return {"command": command, "account": "discord-bot", "user_id": "42", "channel_id": "999",
            "operator": "tony", "is_dm": True, **over}


@pytest.fixture
def parts():
    fed, cx, replies = FakeFed(), FakeCx(), Replies()
    return Commands(fed, cx, reply=replies), fed, cx, replies


async def settle(commands):
    while commands._running:
        await asyncio.gather(*list(commands._running))


# -- /reject ---------------------------------------------------------------------


async def test_reject_rejects_the_card_with_the_reason_and_tells_the_operator(parts):
    commands, fed, _, replies = parts
    card = fed.add(make_card(title="Rig it"))
    await commands.handle(event("reject", ref=card["id"][:8], reason="out of scope"))
    assert fed.db[card["id"]]["state"] == "rejected"
    assert fed.verbs("reject") == [{"card_id": card["id"], "reason": "out of scope"}]
    assert replies.sent == [("discord-bot", "dm:42", f"Rejected {card['id'][:8]} (Rig it): out of scope")]


async def test_reject_without_a_reason_gives_one(parts):
    commands, fed, _, replies = parts
    card = fed.add(make_card())
    await commands.handle(event("reject", ref=card["id"][:8], reason=""))
    assert fed.verbs("reject")[0]["reason"] == "rejected by the recipient"


async def test_a_reason_typed_after_the_ref_is_kept(parts):
    commands, fed, _, _ = parts
    card = fed.add(make_card())
    await commands.handle(event("reject", ref=f"{card['id'][:8]} testing reject", reason=""))
    assert fed.verbs("reject") == [{"card_id": card["id"], "reason": "testing reject"}]


async def test_reject_answers_in_the_channel_the_command_named(parts):
    commands, fed, _, replies = parts
    card = fed.add(make_card())
    await commands.handle(event("reject", ref=card["id"][:8], reply_to="dm:77"))
    assert replies.sent[0][1] == "dm:77"


@pytest.mark.parametrize("ref,why", [("deadbeef", "no card matches"), ("", "usage: /reject")])
async def test_reject_of_nothing_says_so_and_rejects_nothing(parts, ref, why):
    commands, fed, _, replies = parts
    fed.add(make_card())
    await commands.handle(event("reject", ref=ref))
    assert why in replies.sent[0][2] and fed.verbs("reject") == []


async def test_reject_of_an_ambiguous_prefix_asks_for_more(parts):
    commands, fed, _, replies = parts
    fed.add(make_card(id="ab12cd34" + "0" * 24))
    fed.add(make_card(id="ab12cd34" + "1" * 24))
    await commands.handle(event("reject", ref="ab12cd34"))
    assert "matches 2 cards" in replies.sent[0][2] and fed.verbs("reject") == []


async def test_a_refused_reject_is_reported(parts):
    commands, fed, _, replies = parts
    card = fed.add(make_card())
    fed.fail["reject"] = FedError(403, "only the recipient, the claimant or the admin rejects a card")
    await commands.handle(event("reject", ref=card["id"][:8]))
    assert "could not reject" in replies.sent[0][2] and "403" not in replies.sent[0][2]
    assert fed.db[card["id"]]["state"] == "proposed"


# -- /new-agent ------------------------------------------------------------------


async def test_new_agent_starts_a_session_with_remote_control_and_replies_with_its_link(parts):
    commands, _, cx, replies = parts
    await commands.handle(event("new_agent", prompt="hello", reply_to="dm:42"))
    await settle(commands)
    spec = cx.started[0]
    assert (spec["project"], spec["scope"], spec["remote_control"]) == ("awm", "dev", True)
    assert spec["prompt"].startswith("hello\n\n")
    account, channel, text = replies.sent[0]
    assert (account, channel) == ("discord-bot", "dm:42")
    assert "https://claude.ai/code/session_job0001" in text and spec["name"] in text


async def test_new_agent_takes_its_project_scope_and_name_from_the_node(parts, monkeypatch):
    commands, _, cx, _ = parts
    await commands.handle(event("new_agent", prompt=""))
    await settle(commands)
    spec = cx.started[0]
    assert (spec["project"], spec["scope"], spec["remote_control"]) == ("awm", "dev", True)
    assert spec["name"].startswith("agent-")
    assert "Wait for Tony's first instruction" in spec["prompt"]
    monkeypatch.setenv("AWM_REPORTING_NEW_AGENT_PROJECT", "cloud")
    monkeypatch.setenv("AWM_REPORTING_NEW_AGENT_SCOPE", "main")
    await commands.handle(event("new_agent"))
    await settle(commands)
    assert (cx.started[1]["project"], cx.started[1]["scope"]) == ("cloud", "main")


async def test_a_new_agent_is_told_to_rename_itself_once_it_knows_its_work(parts):
    commands, _, cx, _ = parts
    await commands.handle(event("new_agent", prompt="fix the printer"))
    await settle(commands)
    spec = cx.started[0]
    assert spec["name"] in spec["prompt"]
    assert 'reflection(verb="rename"' in spec["prompt"]


async def test_a_link_that_is_not_ready_is_reported_with_the_reason(parts):
    commands, _, cx, replies = parts
    cx.start_reply = {"ok": True, "job": "abcd1234", "link": None,
                      "link_reason": "Remote Control had not connected after 20s"}
    await commands.handle(event("new_agent"))
    await settle(commands)
    assert replies.sent[0][2] == "started abcd1234, link not ready yet: Remote Control had not connected after 20s"


async def test_a_start_cx_refuses_is_reported(parts):
    commands, _, cx, replies = parts
    cx.start_reply = {"ok": False, "reason": "a live session already holds the name"}
    await commands.handle(event("new_agent"))
    await settle(commands)
    assert "could not start an agent: a live session already holds the name" in replies.sent[0][2]


async def test_a_crash_while_starting_is_reported_too(parts):
    commands, _, cx, replies = parts

    async def boom(spec):
        raise RuntimeError("cx fell over")

    cx.start = boom
    await commands.handle(event("new_agent"))
    await settle(commands)
    assert "new_agent failed: cx fell over" in replies.sent[0][2]


async def test_a_slow_start_does_not_hold_up_the_next_command(parts):
    commands, fed, cx, replies = parts
    gate = asyncio.Event()
    real = cx.start

    async def slow(spec):
        await gate.wait()
        return await real(spec)

    cx.start = slow
    card = fed.add(make_card())
    await commands.handle(event("new_agent"))
    await commands.handle(event("reject", ref=card["id"][:8]))
    assert len(replies.sent) == 1 and "Rejected" in replies.sent[0][2]
    gate.set()
    await settle(commands)
    assert len(replies.sent) == 2


# -- what is not for the door ---------------------------------------------------


@pytest.mark.parametrize("ev", [
    {"command": "approve", "operator": "tony", "account": "a", "user_id": "1"},
    {"command": "reject", "operator": "", "ref": "x", "account": "a", "user_id": "1"},
    {"command": "reject", "ref": "x", "account": "a", "user_id": "1"},
    "not a dict", None,
])
async def test_other_commands_and_non_operators_are_ignored(parts, ev):
    commands, fed, cx, replies = parts
    await commands.handle(ev)
    await settle(commands)
    assert replies.sent == [] and fed.calls == [] and cx.started == []


async def test_a_reply_that_cannot_be_sent_does_not_raise(parts):
    commands, fed, _, replies = parts
    replies.error = OSError("discord down")
    card = fed.add(make_card())
    await commands.handle(event("reject", ref=card["id"][:8]))
    assert fed.db[card["id"]]["state"] == "rejected"


async def test_a_command_with_no_account_or_channel_cannot_be_answered_but_still_runs(parts):
    commands, fed, _, replies = parts
    card = fed.add(make_card())
    await commands.handle({"command": "reject", "operator": "t", "ref": card["id"][:8]})
    assert replies.sent == [] and fed.db[card["id"]]["state"] == "rejected"
