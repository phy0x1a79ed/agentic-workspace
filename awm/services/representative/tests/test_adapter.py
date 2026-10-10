"""The door domain: its declared surface and what each verb does."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awm.config import verb_effect
from awm.representative import hub_adapter
from awm.representative.reconcile import Loop

from stubs import FakeCx, make_card, personas, session_row

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

EFFECTS = {"status": "read", "list": "read", "get": "read", "assign": "write"}


def functions() -> dict[str, dict]:
    return {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}


@pytest.fixture
def runtime(monkeypatch, queue):
    cx = FakeCx([session_row("representative", job="r1")])
    queue.record_session("representative", "r1")
    rt = SimpleNamespace(queue=queue, loop=Loop(queue, cx=cx, personas=personas()),
                         subscriber=SimpleNamespace(last_error=None, attached=True),
                         board_problem=None)
    monkeypatch.setattr(hub_adapter, "RUNTIME", rt)
    return rt


def test_every_verb_declares_its_effect():
    declared = functions()
    assert set(declared) == set(EFFECTS)
    for name, effect in EFFECTS.items():
        assert "effect" in declared[name], f"{name} declares no effect"
        assert verb_effect(declared[name]) == effect


def test_tool_names_fold_into_one_door_domain_and_every_verb_has_a_handler():
    for name, spec in functions().items():
        assert spec["tool"] == f"door_{name}"
        assert name in hub_adapter.HANDLERS
    assert set(hub_adapter.HANDLERS) == set(functions())


def test_required_params_match_what_the_handlers_index():
    assert [p["name"] for p in functions()["get"]["params"] if p.get("required")] == ["card_id"]
    assert {p["name"] for p in functions()["assign"]["params"] if p.get("required")} == {"card_id", "agent"}


async def test_status_when_off_says_why_and_touches_nothing(monkeypatch):
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    out = await hub_adapter.status({})
    assert out["enabled"] is False
    assert "station" in out["reason"]
    assert out["role"] == "station"
    assert out["counts"] is None


async def test_status_reports_counts_cursor_and_liveness(runtime, queue):
    queue.enqueue(make_card())
    queue.set_cursor(12)
    out = await hub_adapter.status({})
    assert out["enabled"] is True
    assert (out["role"], out["swarm"], out["cursor"]) == ("fleet", "tony", 12)
    assert out["counts"]["queued"] == 1
    assert out["representative_alive"] is True
    assert out["secretary_alive"] is False
    assert out["board"] == "connected"
    assert out["sessions"]["representative"] == {"alive": True, "state": "running", "reason": "", "job": "r1"}
    assert out["sessions"]["secretary"]["state"] == "missing"


async def test_status_shows_a_login_block_as_not_alive_with_the_reason(runtime):
    runtime.loop.cx.rows[0].update(state="blocked", needs="login required \u2014 run /login")
    out = await hub_adapter.status({})
    assert out["representative_alive"] is False
    assert out["sessions"]["representative"] == {
        "alive": False, "state": "blocked", "reason": "auth_required", "job": "r1"}


async def test_status_when_cx_cannot_answer_says_unknown(runtime):
    runtime.loop.cx.unavailable = True
    out = await hub_adapter.status({})
    assert out["representative_alive"] is None
    assert out["sessions"]["representative"]["state"] == "unknown"


async def test_status_says_connecting_until_the_stream_is_attached(runtime):
    runtime.subscriber.attached = False
    assert (await hub_adapter.status({}))["board"] == "connecting"
    runtime.subscriber.last_error = "board answered 502"
    assert "502" in (await hub_adapter.status({}))["board"]
    runtime.subscriber = None
    runtime.board_problem = "AWM_BOARD_URL is not set on this node"
    assert "AWM_BOARD_URL" in (await hub_adapter.status({}))["board"]


async def test_list_cuts_long_bodies_and_get_returns_them_whole(runtime, queue):
    long_card = make_card(body="x" * 1200, reply_to="a" * 32)
    queue.enqueue(long_card)
    listed = (await hub_adapter.list_cards({}))["cards"][0]
    assert len(listed["body"]) == hub_adapter.LIST_BODY_CHARS
    assert listed["body_len"] == 1200
    assert listed["reply_to"] == "a" * 32
    got = (await hub_adapter.get({"card_id": long_card["id"]}))["card"]
    assert len(got["body"]) == 1200
    assert got["body_len"] == 1200 and got["reply_to"] == "a" * 32


async def test_assigning_a_message_finishes_it(runtime, queue):
    message = make_card(kind="message")
    queue.enqueue(message)
    out = await hub_adapter.assign({"card_id": message["id"], "agent": "worker-1"})
    assert out["card"]["status"] == "done"


async def test_list_and_get_read_the_queue(runtime, queue):
    a, b = make_card(), make_card(priority="urgent")
    queue.enqueue(a)
    queue.enqueue(b)
    out = await hub_adapter.list_cards({})
    assert [c["card_id"] for c in out["cards"]] == [b["id"], a["id"]]
    assert (await hub_adapter.list_cards({"limit": 1}))["cards"][0]["card_id"] == b["id"]
    assert (await hub_adapter.list_cards({"status": "done"}))["cards"] == []
    got = await hub_adapter.get({"card_id": a["id"]})
    assert got["card"]["body"] == "do the thing"
    assert (await hub_adapter.get({"card_id": "0" * 32}))["ok"] is False
    assert (await hub_adapter.list_cards({"status": "nonsense"}))["ok"] is False
    assert (await hub_adapter.list_cards({"limit": "many"}))["ok"] is False


async def test_the_read_verbs_do_not_change_the_queue(runtime, queue):
    card = make_card()
    queue.enqueue(card)
    before = queue.get(card["id"])
    await hub_adapter.list_cards({})
    await hub_adapter.get({"card_id": card["id"]})
    await hub_adapter.status({})
    assert queue.get(card["id"]) == before


async def test_assign_records_the_target_and_marks_the_card_assigned(runtime, queue):
    card = make_card()
    queue.enqueue(card)
    out = await hub_adapter.assign({"card_id": card["id"], "agent": " worker-3 "})
    assert out["ok"] is True
    assert (out["card"]["status"], out["card"]["assigned_to"]) == ("assigned", "worker-3")
    assert queue.get(card["id"])["assigned_to"] == "worker-3"


async def test_assign_refuses_bad_input_and_finished_cards(runtime, queue):
    card = make_card()
    queue.enqueue(card)
    for args in ({}, {"card_id": card["id"]}, {"card_id": card["id"], "agent": "  "},
                 {"card_id": card["id"], "agent": "x" * 500},
                 {"card_id": card["id"], "agent": "two\nlines"},
                 {"card_id": "0" * 32, "agent": "a"}):
        assert (await hub_adapter.assign(args))["ok"] is False, args
    queue.mark(card["id"], "done")
    assert (await hub_adapter.assign({"card_id": card["id"], "agent": "a"}))["ok"] is False
    assert queue.get(card["id"])["status"] == "done"


async def test_the_queue_verbs_refuse_while_the_door_is_off(monkeypatch):
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    for call in (hub_adapter.list_cards({}), hub_adapter.get({"card_id": "a"}),
                 hub_adapter.assign({"card_id": "a", "agent": "b"})):
        assert (await call)["ok"] is False


async def test_status_reports_a_session_waiting_on_the_next_prompt_as_alive(runtime):
    runtime.loop.cx.rows[0].update(state="blocked", needs="rate limited \u2014 wait and retry")
    out = await hub_adapter.status({})
    assert out["representative_alive"] is True
    assert out["sessions"]["representative"] == {
        "alive": True, "state": "waiting", "reason": "rate limited \u2014 wait and retry", "job": "r1"}
