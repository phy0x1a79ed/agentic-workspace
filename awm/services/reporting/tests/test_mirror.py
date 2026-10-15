"""The lifecycle mirror: a claimed task moves its federation card, and a move by hand wins."""

from __future__ import annotations

import time

import pytest

from awm.reporting.federation import FedError
from awm.reporting.ledger import ARCHIVE_AFTER_S

from test_tasks import URL, Env, items, row

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    return Env(tmp_path)


async def claimed(env, **over):
    card = env.federation_card(**over)
    assert (await env.call("claim_task", ref=card["id"][:8]))["ok"]
    return card


def state_of(env, card):
    return env.fed.db[card["id"]]["state"]


def said(env):
    return [body for _, body, _ in env.fed.thread]


async def test_the_plan_posts_a_message_and_leaves_the_card_in_planned(env):
    card = await claimed(env)
    await env.call("hook_plan", plan_text="# The plan\n\nstep one")
    assert state_of(env, card) == "planned" and said(env) == ["plan submitted"]
    assert env.fed.thread[0][2] == 100, "the message is posted for the claimant's session"
    assert env.fed.verbs("status") == []


async def test_a_plan_rewritten_before_approval_does_not_post_again(env):
    await claimed(env)
    for _ in range(3):
        await env.call("hook_plan", plan_text="same text")
    assert said(env) == ["plan submitted"]
    await env.call("hook_plan", plan_text="a revised plan")
    assert said(env) == ["plan submitted", "plan submitted"]


async def test_approval_moves_the_card_to_active_and_says_so(env):
    card = await claimed(env)
    await env.call("hook_plan", plan_text="p")
    await env.call("hook_plan_approved", plan_text="p")
    assert state_of(env, card) == "active" and said(env) == ["plan submitted", "plan approved"]
    assert env.task()["fed_state"] == "active"


async def test_block_review_unblock_and_done_follow_the_ledger(env):
    card = await claimed(env)
    await env.call("hook_plan_approved", plan_text="p")
    await env.call("request_human", reason="need a login", urgent=False)
    assert state_of(env, card) == "blocked"
    await env.call("hook_prompt", prompt="here you go")
    assert state_of(env, card) == "active"
    await env.call("request_human", kind="review", summary="done")
    assert state_of(env, card) == "review"
    await env.tasks.sweep([row("stopped")])
    assert state_of(env, card) == "done" and env.task()["state"] == "done"
    assert env.column(card["note_id"]) == ["Done"]


async def test_a_session_that_ends_before_review_leaves_the_federation_card_where_it_is(env):
    card = await claimed(env)
    await env.call("hook_plan_approved", plan_text="p")
    await env.tasks.sweep([row("stopped")])
    assert env.task()["state"] == "done" and env.column(card["note_id"]) == ["Done"]
    assert state_of(env, card) == "active", "an unfinished task is not reported done to the poster"


async def test_finishing_with_board_complete_first_is_not_a_conflict(env):
    card = await claimed(env)
    await env.call("hook_plan_approved", plan_text="p")
    await env.call("request_human", kind="review", summary="s")
    env.fed.db[card["id"]].update(state="done")  # the agent ran board_complete
    await env.tasks.sweep([row("stopped")])
    assert env.task()["fed_state"] == "done" and env.fed.verbs("status")[-1]["state"] == "review"


async def test_todo_progress_posts_nothing_and_asks_the_board_nothing(env):
    await claimed(env)
    before = len(env.fed.calls)
    await env.call("hook_todos", items=items(("1", "a", "pending")))
    await env.call("hook_todos", items=items(("1", "a", "completed")))
    assert len(env.fed.calls) == before and said(env) == []


async def test_a_move_by_hand_wins_and_the_mirror_does_not_move_it_back(env):
    card = await claimed(env)
    env.fed.db[card["id"]].update(state="blocked")  # Tony dragged it in the GUI
    await env.call("hook_plan_approved", plan_text="p")
    assert state_of(env, card) == "blocked" and env.fed.verbs("status") == []
    assert env.task()["fed_state"] == "blocked", "the mirror adopts where Tony put it"
    await env.call("request_human", kind="review", summary="s")
    assert state_of(env, card) == "review", "the next transition applies again"


async def test_a_manual_move_to_the_state_the_mirror_wants_is_simply_accepted(env):
    card = await claimed(env)
    env.fed.db[card["id"]].update(state="active")
    await env.call("hook_plan_approved", plan_text="p")
    assert env.fed.verbs("status") == [] and env.task()["fed_state"] == "active"
    assert state_of(env, card) == "active"


async def test_a_failed_board_write_leaves_the_task_dirty_and_the_sweep_retries(env):
    card = await claimed(env)
    env.fed.fail["status"] = FedError(503, "board down")
    await env.call("hook_plan_approved", plan_text="p")
    assert env.task()["dirty"] == 1 and state_of(env, card) == "planned"
    assert env.task()["state"] == "active"
    await env.tasks.sweep([row("working")])
    assert env.task()["dirty"] == 0 and state_of(env, card) == "active"


async def test_an_unreachable_board_is_retried_too(env):
    card = await claimed(env)
    env.fed.fail["get"] = FedError(0, "the board could not be reached")
    await env.call("request_human", reason="x", urgent=False)
    assert env.task()["dirty"] == 1
    await env.tasks.sweep([row("working")])
    assert env.task()["dirty"] == 0 and state_of(env, card) == "blocked"


async def test_a_refusal_the_board_will_repeat_is_not_retried_for_ever(env):
    card = await claimed(env)
    env.fed.sticky["status"] = FedError(403, "only the claimant moves a card")
    await env.call("hook_plan_approved", plan_text="p")
    assert env.task()["dirty"] == 0 and state_of(env, card) == "planned"
    calls = len(env.fed.verbs("status"))
    await env.tasks.sweep([row("working")])
    assert len(env.fed.verbs("status")) == calls


async def test_a_plain_task_never_touches_the_federation_board(env):
    await env.call("hook_plan_approved", plan_text="p")
    await env.call("request_human", kind="review", summary="s")
    await env.tasks.sweep([row("stopped")])
    assert env.fed.calls == []


async def test_a_message_card_claim_mirrors_nothing(env):
    card = env.federation_card(kind="message")
    await env.call("claim_task", ref=card["id"][:8])
    await env.call("hook_plan_approved", plan_text="p")
    assert env.fed.verbs("status") == [] and said(env) == []
    assert env.column(card["note_id"]) == ["Active"]


# -- moves on either board -------------------------------------------------------


async def test_a_card_dragged_to_done_on_the_swarm_board_closes_the_task_and_mirrors_done(env):
    card = await claimed(env)
    env.tri.notes[card["note_id"]]["parents"] = [env.ids["Done"]]
    await env.tasks.sweep([row("working")])
    assert env.task()["state"] == "done" and state_of(env, card) == "done"


async def test_a_card_dragged_to_rejected_on_the_swarm_board_rejects_the_federation_card(env):
    card = await claimed(env)
    env.tri.notes[card["note_id"]]["parents"] = [env.ids["Rejected"]]
    await env.tasks.sweep([row("working")])
    assert env.task()["state"] == "rejected" and state_of(env, card) == "rejected"
    assert env.fed.verbs("reject")[0]["reason"] == "rejected from the Swarm board"


async def test_a_card_rejected_on_the_federation_board_closes_the_task_into_the_rejected_column(env):
    card = await claimed(env)
    env.fed.db[card["id"]].update(state="rejected")
    await env.tasks.card_closed(card["id"], "rejected")
    task = env.task()
    assert task["state"] == "rejected" and task["fed_state"] == "rejected" and task["closed"]
    assert env.column(card["note_id"]) == ["Rejected"]
    assert env.fed.verbs("status") == [] and env.fed.verbs("reject") == []


async def test_a_card_done_on_the_federation_board_closes_the_task_as_done(env):
    card = await claimed(env)
    await env.tasks.card_closed(card["id"], "done")
    assert env.task()["state"] == "done" and env.column(card["note_id"]) == ["Done"]


async def test_a_finished_card_nothing_holds_is_ignored(env):
    await env.tasks.card_closed("f" * 32, "rejected")
    assert env.ledger.open() == []


async def test_a_node_without_the_stream_finds_a_rejection_in_the_sweep(env, monkeypatch):
    monkeypatch.setenv("AWM_REPORTING_CATCHUP_S", "0.001")
    card = await claimed(env)
    env.fed.db[card["id"]].update(state="rejected")
    await env.tasks.sweep([row("working")])
    assert env.task()["state"] == "rejected"


async def test_the_sweep_asks_about_a_card_only_once_per_catch_up_period(env):
    card = await claimed(env)
    await env.tasks.sweep([row("working")])
    await env.tasks.sweep([row("working")])
    assert len(env.fed.verbs("get")) == 1 and card


# -- archive ---------------------------------------------------------------------


async def test_a_done_card_is_archived_after_fourteen_days_and_not_before(env):
    card = await claimed(env)
    await env.tasks.sweep([row("stopped")])
    task = env.task()
    assert "archived" not in env.tri.notes[card["note_id"]]["labels"]
    env.ledger.update(task["id"], closed=time.time() - ARCHIVE_AFTER_S + 3600)
    await env.tasks.sweep([])
    assert "archived" not in env.tri.notes[card["note_id"]]["labels"]
    env.ledger.update(task["id"], closed=time.time() - ARCHIVE_AFTER_S - 3600)
    await env.tasks.sweep([])
    assert "archived" in env.tri.notes[card["note_id"]]["labels"] and env.task()["shown"] == "archived"
    calls = len(env.tri.calls)
    await env.tasks.sweep([])
    assert [fn for fn, a in env.tri.calls[calls:] if a.get("labels") == {"archived": ""}] == []


async def test_a_card_the_service_made_is_archived_but_tonys_own_note_never_is(env):
    await env.call("hook_plan_approved", plan_text="p")
    made = env.task()
    await env.tasks.sweep([row("stopped")])
    env.ledger.update(made["id"], closed=time.time() - ARCHIVE_AFTER_S - 10)
    note, _ = env.tonys_note()
    env.records[300] = {**env.records[100], "pid": 300, "jobId": "job00003", "sessionId": "s3"}
    await env.call("claim_task", pid=300, url=URL.format(note))
    await env.tasks.sweep([row("stopped", "job00003")])
    env.ledger.update(env.task("job00003")["id"], closed=time.time() - ARCHIVE_AFTER_S - 10)
    await env.tasks.sweep([])
    assert "archived" in env.tri.notes[made["note_id"]]["labels"]
    assert "archived" not in env.tri.notes[note]["labels"]
