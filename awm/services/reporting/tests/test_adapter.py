"""The reporting domain: its declared surface and what each verb does."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awm.config import verb_effect
from awm.reporting import hub_adapter
from awm.reporting.ledger import Ledger
from awm.reporting.reconcile import Loop

from stubs import FakeCx, make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

EFFECTS = {"status": "read", "board_ensure": "write", "ops_post": "write", "convert": "write",
           **dict.fromkeys(hub_adapter.TASK_VERBS, "write")}


def functions() -> dict[str, dict]:
    return {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}


@pytest.fixture
def runtime(monkeypatch, queue):
    rt = SimpleNamespace(queue=queue, loop=Loop(FakeCx()),
                         tasks=SimpleNamespace(node="altair", subscription=None, ledger=Ledger(queue.path)),
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


PUBLISHED = {
    "status": "operations_status", "claim_task": "operations_task_claim",
    "track_task": "operations_task_track",
    "board_watch": "operations_board_watch", "board_unwatch": "operations_board_unwatch",
    "request_human": "operations_request_human", "hook_plan": "operations_hook_plan",
    "hook_plan_approved": "operations_hook_plan_approved", "hook_todos": "operations_hook_todos",
    "hook_prompt": "operations_hook_prompt", "hook_session_clear": "operations_hook_session_clear",
    "ops_post": "operations_ops_post", "board_ensure": "operations_board_ensure",
    "convert": "operations_convert",
}
HIDDEN = {"hook_plan", "hook_plan_approved", "hook_todos", "hook_prompt", "hook_session_clear", "ops_post",
          "board_ensure", "convert"}


def test_the_verbs_publish_into_the_operations_tool_and_every_one_has_a_handler():
    assert {name: spec["tool"] for name, spec in functions().items()} == PUBLISHED
    assert set(hub_adapter.HANDLERS) == set(functions())


def test_the_queue_and_persona_verbs_are_gone():
    published = set(PUBLISHED.values())
    assert not any(t.startswith("operations_queue_") for t in published)
    text = " ".join(f["description"] for f in functions().values()) + hub_adapter.API_MANIFEST["description"]
    assert "representative" not in text and "secretary" not in text


def test_the_manifest_opts_into_the_operations_tool():
    toml = (Path(hub_adapter.__file__).parents[2] / "service.toml").read_text()
    assert 'tool = "operations"' in toml and 'tier = "on"' in toml


def test_hook_ops_and_operator_verbs_leave_mcp_and_the_rest_stay():
    hidden = {name for name, spec in functions().items() if spec.get("surfaces") == ["cli", "http"]}
    assert hidden == HIDDEN
    assert all("surfaces" not in spec for name, spec in functions().items() if name not in HIDDEN)


def test_claim_takes_a_ref_or_the_old_url_and_neither_is_required():
    params = {p["name"]: p for p in functions()["claim_task"]["params"]}
    assert set(params) == {"ref", "url"} and not any(p.get("required") for p in params.values())
    description = functions()["claim_task"]["description"]
    assert "scope create" in description and "sessions" in description and "board_complete" in description


def test_watch_takes_a_card_or_the_posted_flag():
    params = {p["name"] for p in functions()["board_watch"]["params"]}
    assert params == {"card_id", "posted_by_me", "project", "swarm"}


async def test_convert_writes_only_when_dry_run_is_explicitly_false(monkeypatch):
    seen = []

    async def convert(dry_run):
        seen.append(dry_run)
        return {"ok": True}

    monkeypatch.setattr(hub_adapter, "RUNTIME", SimpleNamespace(tasks=SimpleNamespace(convert=convert)))
    for args in ({}, {"dry_run": True}, {"dry_run": None}, {"dry_run": "false"}, {"dry_run": False}):
        await hub_adapter.convert(args)
    assert seen == [True, True, True, True, False]
    assert (await hub_adapter.convert({"dry_run": False}, as_="peer:x"))["ok"] is False


def test_request_human_takes_a_kind_and_old_arguments_still_pick_one():
    params = {p["name"]: p for p in functions()["request_human"]["params"]}
    assert set(params) == {"kind", "reason", "urgent", "summary", "bundle"}
    assert not any(p.get("required") for p in params.values())




async def test_status_when_off_says_why_and_touches_nothing(monkeypatch):
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    out = await hub_adapter.status({})
    assert out["enabled"] is False
    assert "station" in out["reason"]
    assert out["role"] == "station"
    assert out["counts"] is None








async def test_status_reports_the_cursor_counts_and_board_connection(runtime, queue):
    queue.enqueue(make_card())
    queue.set_cursor(12)
    out = await hub_adapter.status({})
    assert out["enabled"] is True
    assert (out["role"], out["swarm"], out["cursor"]) == ("fleet", "tony", 12)
    assert out["counts"]["proposed"] == 1 and out["board"] == "connected"
    assert out["reconcile"]["running"] is False
    assert not {"sessions", "representative_alive", "secretary_alive"} & set(out)


async def test_status_says_connecting_until_the_stream_is_attached(runtime):
    runtime.subscriber.attached = False
    assert (await hub_adapter.status({}))["board"] == "connecting"
    runtime.subscriber.last_error = "board answered 502"
    assert "502" in (await hub_adapter.status({}))["board"]
    runtime.subscriber = None
    runtime.board_problem = "AWM_BOARD_URL is not set on this node"
    assert "AWM_BOARD_URL" in (await hub_adapter.status({}))["board"]


















async def test_board_ensure_is_an_operator_verb_and_reports_a_missing_awm_note(monkeypatch):
    from awm.reporting.swarmboard import SwarmBoard
    from stubs import FakeTrilium

    tri = FakeTrilium()
    monkeypatch.setattr(hub_adapter, "RUNTIME", SimpleNamespace(board=SwarmBoard(tri)))
    assert (await hub_adapter.board_ensure({}, as_="someone"))["ok"] is False
    assert "AWM" in (await hub_adapter.board_ensure({}))["error"]
    tri.add("AWM", type="book")
    out = await hub_adapter.board_ensure({})
    assert out["ok"] is True and set(out["columns"]) == {
        "Proposed", "Planned", "Active", "Blocked", "Pending review", "Done", "Rejected"}


async def test_ops_post_keeps_one_managed_note_per_kind_and_node_under_a_private_ops_book(monkeypatch):
    from awm.reporting.swarmboard import SwarmBoard
    from stubs import FakeTrilium

    tri = FakeTrilium()
    awm = tri.add("AWM", type="book")
    monkeypatch.setattr(hub_adapter, "RUNTIME", SimpleNamespace(board=SwarmBoard(tri)))
    args = {"kind": "password", "node": "altair", "title": "Login password", "body": "hunter2"}
    assert (await hub_adapter.ops_post(args))["ok"] is False
    await hub_adapter.RUNTIME.board.ensure()
    first = await hub_adapter.ops_post(args)
    again = await hub_adapter.ops_post({**args, "body": "correct horse"})
    other = await hub_adapter.ops_post({**args, "node": "capella"})
    assert first["note_id"] == again["note_id"] != other["note_id"]
    note = tri.notes[first["note_id"]]
    assert note["title"] == "password - altair" and "correct horse" in note["content"] and "hunter2" not in note["content"]
    assert set(note["labels"]) == {"agentManaged", "readOnly", "changed"}
    assert note["content"].startswith(f"Changed: {note['labels']['changed']}\n\n## Login password")
    ops = tri.parents(first["note_id"])[0]
    assert tri.notes[ops]["title"] == "Ops" and tri.parents(ops) == [awm] and tri.notes[ops]["labels"] == {"swarmOps": ""}


async def test_ops_post_refuses_bad_arguments_and_foreign_or_edge_callers(monkeypatch):
    from awm.reporting.swarmboard import SwarmBoard
    from stubs import FakeTrilium

    tri = FakeTrilium()
    tri.add("AWM", type="book")
    monkeypatch.setattr(hub_adapter, "RUNTIME", SimpleNamespace(board=SwarmBoard(tri)))
    await hub_adapter.RUNTIME.board.ensure()
    good = {"kind": "password", "node": "altair", "title": "t", "body": "b"}
    assert (await hub_adapter.ops_post({**good, "body": " "}))["ok"] is False
    assert (await hub_adapter.ops_post({**good, "kind": "two\nlines"}))["ok"] is False
    assert (await hub_adapter.ops_post(good, as_="user:tony"))["ok"] is False
    monkeypatch.setattr(hub_adapter, "caller_peer", lambda as_: "mira")
    monkeypatch.setattr(hub_adapter, "peer_relation", lambda node: "foreign")
    assert (await hub_adapter.ops_post(good, as_="peer:mira"))["ok"] is False
    monkeypatch.setattr(hub_adapter, "peer_relation", lambda node: "domestic")
    assert (await hub_adapter.ops_post(good, as_="peer:mira"))["ok"] is True


async def test_status_reports_the_task_part_even_with_the_front_door_off(monkeypatch, queue):
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    ledger = Ledger(queue.path)
    task, _ = ledger.ensure("altair", "j", "bg")
    ledger.update(task["id"], dirty=1)
    monkeypatch.setattr(hub_adapter, "RUNTIME", SimpleNamespace(
        queue=queue, tasks=SimpleNamespace(node="altair", subscription=None, ledger=ledger),
        board_problem=None, subscriber=SimpleNamespace(last_error=None, attached=True)))
    out = await hub_adapter.status({})
    assert out["enabled"] is False and out["tasks"] == {"open": 1, "unsynced": 1, "journals": None}
    assert out["watch_relay"] == "connected"
