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
    assert out["panel_url"] == f"https://auth.tony-xy-liu.com/panel?task={out['task_id']}"
    got = await h.call("task_get", task_id=out["task_id"])
    assert got["panel_url"] == out["panel_url"] and got["task"]["task_id"] == out["task_id"]
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


async def test_an_isolated_session_is_known_by_the_id_the_door_stamped(h):
    """An isolated session has no host pid: its own-session verbs read _caller_session_id."""
    out = await h.front.call("task_open", {"_caller_session_id": B, "title": "boxed work"})
    assert h.store.task_get(out["task_id"])["sessions"][0]["session_id"] == B


async def test_a_plan_opens_a_planned_card_and_approval_moves_it_to_working(h):
    out = await h.call("hook_plan", 11, session_id=A, plan_text="# Fix the tunnel\n\nsteps")
    assert out["opened"] is True and out["blocked"] is True and out["task_id"] in out["notice"]
    card = h.store.task_get(out["task_id"])
    assert card["title"] == "Fix the tunnel" and (card["section"], card["status"]) == (
        "needs_you", "planned")
    assert card["meta"]["plan"].startswith("# Fix the tunnel")
    again = await h.call("hook_plan", 11, session_id=A, plan_text="# Fix the tunnel v2")
    assert again["task_id"] == out["task_id"] and again["opened"] is False
    done = await h.call("hook_plan_approved", 11, session_id=A, plan_text="# Fix the tunnel v3")
    card = h.store.task_get(out["task_id"])
    assert done["blocked"] is False and card["section"] == "working"
    assert card["meta"]["plan"] == "# Fix the tunnel v3"
    assert (await h.call("hook_prompt", 11, prompt="go"))["moved"] is False


async def test_todos_show_on_the_card_and_need_one(h):
    items = [{"id": "1", "subject": "port hooks", "status": "in_progress"}]
    assert (await h.call("hook_todos", 11, session_id=A, items=items))["tracked"] is False
    t = (await h.call("track_task", 11, title="cutover"))["task_id"]
    assert (await h.call("hook_todos", 11, session_id=A, items=items))["tracked"] is True
    assert h.store.task_get(t)["meta"]["todos"] == items
    assert (await h.call("hook_todos", 11, session_id=A, items="x"))["ok"] is False


async def test_track_task_returns_the_card_or_opens_one(h):
    first = await h.call("track_task", 11, title="cutover work")
    assert first["opened"] is True and "notice" in first
    assert h.store.task_get(first["task_id"])["section"] == "working"
    again = await h.call("track_task", 11, title="other")
    assert again == {"ok": True, "task_id": first["task_id"], "opened": False}


async def test_a_clear_takes_the_old_session_off_its_card(h):
    t = (await h.call("track_task", 11, title="before the clear"))["task_id"]
    new = "cc" + "c" * 32
    PIDS[33] = {"ok": True, "session_id": new, "job": "job-a", "name": "fix-tunnel"}
    try:
        out = await h.call("hook_session_clear", 33, new_session_id=new)
        assert out["ended"] == A and out["task_id"] == t
        assert {s["session_id"]: s["active"] for s in h.store.task_sessions(t)} == {A: False}
        assert (await h.call("task_for_session", None, session_id=new))["task_id"] is None
        plan = await h.call("hook_plan", 33, session_id=new, plan_text="# Next")
        assert plan["opened"] is True and plan["task_id"] != t
    finally:
        PIDS.pop(33)


async def test_claim_moves_a_proposed_card_to_working(h):
    t = h.store.card_action("note_new", None, {"title": "idea"})["task_id"]
    assert (await h.call("claim_task", 11))["ok"] is False
    out = await h.call("claim_task", 11, task_id=f"claim task {t}")
    assert out["ok"] and out["section"] == "working"
    card = h.store.task_get(t)
    assert card["kind"] == "task" and card["sessions"][0]["role"] == "main"
    h.store.task_report(t, A, "done")
    assert (await h.call("claim_task", 22, task_id=t))["ok"] is False


async def test_messages_post_and_read_on_a_card(h):
    t = (await h.call("track_task", 11, title="deploy"))["task_id"]
    out = await h.call("swarm_message_post", None, card_id=t, body="deploy passed")
    assert out["ok"] and out["seq"] == 1
    await h.call("swarm_message_post", 22, card_id=t, body="seen")
    got = await h.call("swarm_messages", None, card_id=t)
    assert [(m["author"], m["body"]) for m in got["messages"]] == [
        ("a service", "deploy passed"), ("other", "seen")]
    later = await h.call("swarm_messages", None, card_id=t, after=1)
    assert [m["body"] for m in later["messages"]] == ["seen"]
    assert (await h.call("swarm_message_post", None, card_id=t, body=" "))["ok"] is False


async def test_an_urgent_block_reminds_tony_once(h, monkeypatch):
    now = [1000.0]
    h.front.clock = lambda: now[0]
    monkeypatch.setenv("AWM_OPERATIONS_REMIND_S", "60")
    out = await h.call("request_human", 11, kind="blocked", reason="key expired", urgent=True)
    assert await h.front.remind_due() == 0
    now[0] += 61
    assert await h.front.remind_due() == 1
    assert h.dms[-1].startswith("Still waiting:") and "key expired" in h.dms[-1]
    now[0] += 3600
    assert await h.front.remind_due() == 0 and len(h.dms) == 2
    await h.call("request_human", 11, kind="blocked", reason="again", urgent=True)
    await h.call("hook_prompt", 11, prompt="here you go")
    now[0] += 61
    assert await h.front.remind_due() == 0
    assert h.store.task_get(out["task_id"])["section"] == "working"


async def test_a_board_post_becomes_one_proposed_card_and_its_thread_reaches_the_session(h):
    post = {"board_card": "c0ffee", "sender": "caviar/agent", "title": "help with x", "body": "details"}
    assert (await h.call("intake_post", 11, **post))["ok"] is False, "sessions cannot post intake"
    out = await h.call("intake_post", None, **post)
    assert out["opened"] is True and f"claim task {out['task_id']}" in h.dms[-1]
    card = h.store.task_get(out["task_id"])
    assert (card["section"], card["tags"], card["title"]) == ("proposed", ["board"],
                                                              "caviar/agent: help with x")
    assert [m["body"] for m in h.store.task_messages(out["task_id"])] == ["details"]
    again = await h.call("intake_post", None, **post)
    assert again == {"ok": True, "task_id": out["task_id"], "opened": False} and len(h.dms) == 1
    await h.call("claim_task", 11, task_id=out["task_id"])
    said = await h.call("intake_message", None, board_card="c0ffee", sender="caviar/agent",
                        body="any news?")
    assert said["told"] is True and h.told[-1][1] == "fix-tunnel"
    reply = await h.call("intake_post", None, board_card="c0ffee2", sender="caviar/agent",
                         title="re: help", body="more", reply_to="c0ffee")
    assert reply["task_id"] == out["task_id"]
    assert [m["body"] for m in h.store.task_messages(out["task_id"])][-1] == "more"
    assert (await h.call("intake_message", None, board_card="nope", sender="x",
                         body="y"))["task_id"] is None
