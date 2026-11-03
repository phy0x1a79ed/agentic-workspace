"""The session verbs: request_human, the urgent DM, Tony's prompt, approvals and Arbutus."""

from __future__ import annotations

import pytest

from awm.operations.door import LocalClient
from awm.operations.front import Front
from awm.operations.home import Home
from awm.operations.relay import Relay

A, B = "cc" + "a" * 32, "cc" + "b" * 32
PIDS = {11: {"ok": True, "session_id": A, "job": "job-a", "name": "fix-tunnel"},
        22: {"ok": True, "session_id": B, "job": "job-b", "name": "other"}}


class Harness:
    def __init__(self, tmp_path, env) -> None:
        ap, store, _ = env
        self.ap, self.store = ap, store
        self.dms: list[str] = []
        self.told: list[tuple] = []
        self.relay = Relay("mizar", tmp_path / "r.db", LocalClient(Home(store, ap), "mizar"))
        self.front = Front(self.relay, "mizar", self.session_of, self.tell, self.dm)

    async def session_of(self, pid):
        return PIDS.get(pid)

    async def tell(self, node, job, text):
        self.told.append((node, job, text))
        return True

    async def dm(self, text):
        self.dms.append(text)
        return True

    async def call(self, verb, pid=None, **args):
        if pid is not None:
            args["_caller_pid"] = pid
        return await self.front.call(verb, args)


@pytest.fixture
def h(tmp_path, env):
    return Harness(tmp_path, env)


async def test_blocked_opens_a_card_and_urgent_sends_a_dm(h):
    out = await h.call("request_human", 11, kind="blocked", reason="needs the VPN key", urgent=True)
    assert out["ok"] and out["section"] == "needs_you" and out["status"] == "blocked"
    assert out["notified"] is True and "needs the VPN key" in h.dms[0]
    assert "fix-tunnel" in h.dms[0] and "job-a" in h.dms[0]
    card = h.store.task_get(out["task_id"])
    assert card["meta"]["reason"] == "needs the VPN key"
    assert card["sessions"][0]["session_id"] == A
    out = await h.call("request_human", 11, kind="review", summary="look at the diff")
    assert out["status"] == "review" and len(h.dms) == 1


async def test_tonys_prompt_moves_the_card_to_working(h):
    out = await h.call("request_human", 11, kind="blocked", reason="which port?")
    assert (await h.call("hook_prompt", 22, prompt="hi"))["moved"] is False
    moved = await h.call("hook_prompt", 11, prompt="use 2201")
    assert moved["moved"] is True
    assert h.store.task_get(out["task_id"])["section"] == "working"
    assert (await h.call("hook_prompt", 11, prompt="thanks"))["moved"] is False


async def test_report_needs_a_task_and_a_known_state(h):
    assert (await h.call("task_report", 11, state="done"))["ok"] is False
    t = (await h.call("task_open", 11, title="x"))["task_id"]
    assert (await h.call("task_report", 11, state="sideways"))["ok"] is False
    assert (await h.call("task_report", 11, state="done"))["task_id"] == t


async def test_join_tells_the_main_session(h):
    t = (await h.call("task_open", 11, title="main work", name="fix-tunnel"))["task_id"]
    out = await h.call("task_join", 22, task_id=t)
    assert out["main_session_id"] == A and out["main_notified"] is True
    assert h.told[0][:2] == ("mizar", "fix-tunnel")


async def test_only_the_requesting_session_cancels(h):
    r = await h.call("approval_request", 11, kind="vault_hold", reason="r", session_id=B)
    assert h.ap.approval_get(r["request_id"])["requester_session"] == A, "a named session is ignored"
    assert (await h.call("approval_cancel", 22, request_id=r["request_id"]))["ok"] is False
    assert (await h.call("approval_cancel", None, request_id=r["request_id"]))["ok"] is False
    lie = await h.call("approval_cancel", 22, request_id=r["request_id"], session_id=A)
    assert lie["ok"] is False and "does not match" in lie["error"]
    out = await h.call("approval_cancel", 11, request_id=r["request_id"])
    assert out["ok"] and out["status"] == "cancelled"


async def test_session_ids_pass_through_the_r9_parser(h):
    uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    out = await h.call("task_open", None, title="from a service", session_id=uuid)
    got = await h.call("task_for_session", None, session_id=A)
    assert got["task_id"] == out["task_id"]
    assert (await h.call("task_for_session", None, session_id="job0001"))["ok"] is False


class FakeArbutus:
    def __init__(self) -> None:
        self.attached: list[tuple] = []

    async def open(self, bundle, *, agent, node, job):
        return {"ref": "a1b2c3", "name": bundle}

    def attach(self, ref, task_id):
        self.attached.append((ref, task_id))


async def test_arbutus_approval_blocks_the_card_until_slack_decides(h):
    assert (await h.call("request_human", 11, kind="approval", bundle="grow"))["ok"] is False
    h.front.arbutus = FakeArbutus()
    out = await h.call("request_human", 11, kind="approval", bundle="grow")
    assert out["approval"] == "a1b2c3" and out["status"] == "blocked"
    assert h.front.arbutus.attached == [("a1b2c3", out["task_id"])]
    assert "Tony or Ryan" in h.store.task_get(out["task_id"])["meta"]["reason"]
    await h.front.arbutus_decided(out["task_id"], "Approved by ryan in Slack.")
    assert h.store.task_get(out["task_id"])["section"] == "working"
