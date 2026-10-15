"""task_claim on a federation card: claim by reference, Planned on both boards, scope and sessions."""

from __future__ import annotations

import pytest

from awm.reporting.federation import FedError

from stubs import make_card
from test_tasks import BG, TTY, URL, Env

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    return Env(tmp_path)


def ref(card):
    return card["id"][:8]


async def test_claiming_by_short_id_claims_on_the_board_as_the_caller_and_shows_it_planned_on_both(env):
    card = env.federation_card(title="Rig the thing", project="factorio",
                               sender={"swarm": "beta", "principal": "op", "party": "p"})
    out = await env.call("claim_task", ref=ref(card))
    assert out["ok"] and out["state"] == "planned" and out["ref"] == ref(card)
    assert env.fed.verbs("claim") == [{"card_id": card["id"]}]
    assert [pid for fn, _, pid in env.fed.calls if fn == "claim"] == [100], "the board stamps the caller"
    assert env.fed.db[card["id"]]["state"] == "planned"
    assert env.fed.db[card["id"]]["owner"] == "tony/altair/opal"
    task = env.task()
    assert (task["card_id"], task["fed_state"], task["note_id"]) == (card["id"], "planned", card["note_id"])
    assert task["title"] == "Rig the thing" and task["owned"] == 0
    assert env.column(card["note_id"]) == ["Planned"]
    assert "Federation board" in [env.tri.notes[p]["title"] for p in _grandparents(env, card["note_id"])]
    assert [e["kind"] for e in env.ledger.events(task["id"])] == ["created", "claimed"]


def _grandparents(env, note):
    return [gp for p in env.tri.parents(note) for gp in env.tri.parents(p)]


async def test_the_card_note_keeps_its_federation_place_and_shows_the_chips(env):
    card = env.federation_card(project="factorio", owner=None)
    await env.call("claim_task", ref=ref(card))
    labels = env.tri.notes[card["note_id"]]["labels"]
    assert labels["cardFrom"] == "beta" and labels["cardTo"] == "tony"
    assert labels["project"] == "factorio" and labels["owner"] == "tony/altair/opal"
    assert env.tri.notes[card["note_id"]]["content"] == "the card body"
    chips = env.tri.notes[env.ids["board"]]["labels"]
    assert {"label:cardFrom", "label:cardTo", "label:project", "label:owner"} <= set(chips)


async def test_the_assignee_is_the_callers_job_and_the_agent_name(env):
    card = env.federation_card()
    out = await env.call("claim_task", ref=ref(card))
    row = env.queue.get(card["id"])
    assert (row["status"], row["assigned_to"], row["assigned_job"]) == ("assigned", "opal", "job00001")
    assert out["wakes_on_messages"] is True


async def test_an_interactive_session_can_claim_but_has_no_job_to_wake(env):
    card = env.federation_card()
    out = await env.call("claim_task", pid=200, ref=ref(card))
    row = env.queue.get(card["id"])
    assert out["ok"] and out["wakes_on_messages"] is False
    assert (row["assigned_to"], row["assigned_job"]) == ("tty-agent", None)


@pytest.mark.parametrize("how", ["full", "prefix", "note"])
async def test_a_full_id_a_unique_prefix_and_the_trilium_link_name_the_same_card(env, how):
    card = env.federation_card()
    env.federation_card()
    given = {"full": card["id"], "prefix": card["id"][:8], "note": URL.format(card["note_id"])}[how]
    out = await env.call("claim_task", ref=given)
    assert out["ok"] and out["card_id"] == card["id"]


async def test_the_line_from_the_message_pastes_whole(env):
    card = env.federation_card()
    out = await env.call("claim_task", ref=f"claim task {ref(card)} {URL.format(card['note_id'])}")
    assert out["ok"] and out["card_id"] == card["id"]


async def test_an_ambiguous_prefix_is_refused_and_claims_nothing(env):
    a = env.federation_card(id="ab12cd34" + "0" * 24)
    env.federation_card(id="ab12cd34" + "1" * 24)
    out = await env.call("claim_task", ref="ab12cd34")
    assert out["ok"] is False and "matches 2 cards" in out["error"]
    assert env.fed.verbs("claim") == [] and env.task() is None and a


async def test_a_ref_that_names_no_card_is_an_error_and_not_a_note(env):
    env.federation_card()
    out = await env.call("claim_task", ref="deadbeef")
    assert out["ok"] is False and "no federation card matches" in out["error"]


async def test_a_card_held_by_another_swarm_answers_with_the_conflict_and_leaves_no_task(env):
    card = env.federation_card(recipient="open")
    env.fed.fail["claim"] = FedError(409, "the card is already held")
    out = await env.call("claim_task", ref=ref(card))
    assert out["ok"] is False and "another swarm" in out["error"]
    assert env.task() is None and env.queue.get(card["id"]) is None
    assert env.column(card["note_id"]) == []


async def test_a_message_card_is_never_claimed_on_the_board(env):
    card = env.federation_card(kind="message", title="fyi")
    out = await env.call("claim_task", ref=ref(card))
    task = env.task()
    assert out["ok"] and env.fed.verbs("claim") == []
    assert (task["card_id"], task["fed_state"]) == (card["id"], None)
    assert env.column(card["note_id"]) == ["Planned"]
    assert env.queue.get(card["id"])["status"] == "assigned"


async def test_claiming_again_from_the_same_job_changes_nothing(env):
    card = env.federation_card()
    await env.call("claim_task", ref=ref(card))
    await env.call("hook_plan_approved", plan_text="p")
    again = await env.call("claim_task", ref=card["id"])
    assert again["ok"] and again["state"] == "active"
    assert len(env.fed.verbs("claim")) == 1 and len(env.ledger.open()) == 1


async def test_a_second_job_is_refused_while_the_card_is_held(env):
    card = env.federation_card()
    await env.call("claim_task", ref=ref(card))
    env.records[300] = {**BG, "pid": 300, "jobId": "job00002", "sessionId": "sess-3"}
    out = await env.call("claim_task", pid=300, ref=ref(card))
    assert out["ok"] is False and "already held here by job00001" in out["error"]


async def test_a_card_released_to_proposed_can_be_claimed_by_another_job(env):
    card = env.federation_card()
    await env.call("claim_task", ref=ref(card))
    env.fed.db[card["id"]].update(state="proposed", claimant=None, owner=None)
    env.records[300] = {**BG, "pid": 300, "jobId": "job00002", "sessionId": "sess-3"}
    out = await env.call("claim_task", pid=300, ref=ref(card))
    assert out["ok"] and env.ledger.find("altair", "job00001")["state"] == "cancelled"
    assert env.queue.get(card["id"])["assigned_job"] == "job00002"


async def test_a_card_another_node_of_this_swarm_holds_is_refused(env):
    card = env.federation_card(state="active", claimant="tony", owner="tony/capella/jo")
    out = await env.call("claim_task", ref=ref(card))
    assert out["ok"] is False and "tony/capella/jo" in out["error"] and "Proposed" in out["error"]
    assert env.fed.verbs("claim") == []


async def test_a_card_this_node_held_before_is_taken_over(env):
    card = env.federation_card(state="active", claimant="tony", owner="tony/altair/old-agent")
    out = await env.call("claim_task", ref=ref(card))
    assert out["ok"] and out["card_state"] == "planned"
    assert env.queue.get(card["id"])["assigned_job"] == "job00001"


async def test_one_session_cannot_hold_two_cards(env):
    a, b = env.federation_card(), env.federation_card()
    await env.call("claim_task", ref=ref(a))
    out = await env.call("claim_task", ref=ref(b))
    assert out["ok"] is False and "already works on card" in out["error"]


async def test_a_session_with_a_task_of_its_own_gives_it_to_the_card(env):
    await env.call("hook_plan", plan_text="# Mine\n\nx")
    card = env.federation_card()
    out = await env.call("claim_task", ref=ref(card))
    assert out["ok"] and len(env.ledger.open()) == 1
    task = env.task()
    assert task["note_id"] == card["note_id"] and task["card_id"] == card["id"] and task["owned"] == 0


async def test_a_card_the_board_reports_without_a_note_cannot_be_shown(env):
    card = env.federation_card()
    env.fed.db[card["id"]]["note_id"] = None
    out = await env.call("claim_task", ref=ref(card))
    assert out["ok"] is False and "note" in out["error"]


async def test_a_plain_note_link_still_works_when_the_board_cannot_be_asked(env):
    note, _ = env.tonys_note()
    env.fed.sticky["list"] = FedError(0, "board unreachable")
    out = await env.call("claim_task", url=URL.format(note))
    assert out["ok"] and env.task()["card_id"] is None and env.task()["note_id"] == note


async def test_a_card_ref_when_the_board_cannot_be_asked_says_so(env):
    env.fed.sticky["list"] = FedError(0, "board unreachable")
    out = await env.call("claim_task", ref="1a2b3c4d")
    assert out["ok"] is False and "cannot look up" in out["error"]


# -- the prompt hook ------------------------------------------------------------


async def test_a_first_prompt_that_says_claim_task_claims_the_card(env):
    card = env.federation_card()
    out = await env.call("hook_prompt", prompt=f"claim task {ref(card)} {URL.format(card['note_id'])}")
    assert out["ok"] and env.task()["card_id"] == card["id"]
    assert len(env.fed.verbs("claim")) == 1
    await env.call("claim_task", ref=ref(card))  # the agent then runs the verb itself
    assert len(env.fed.verbs("claim")) == 1, "the same job claims once"


async def test_a_claim_task_prompt_with_no_card_ref_in_it_claims_nothing(env):
    env.federation_card()
    for prompt in ("please claim task list management for me", "claim task xyz"):
        assert (await env.call("hook_prompt", prompt=prompt))["ok"]
    assert env.task() is None and env.fed.verbs("claim") == []


async def test_the_card_url_in_a_claim_prompt_takes_the_card_path_and_not_the_note_path(env):
    card = env.federation_card()
    out = await env.call("hook_prompt", prompt=f"claim {URL.format(card['note_id'])}")
    assert out["ok"] and env.task()["card_id"] == card["id"] and env.fed.verbs("claim")


# -- where to work --------------------------------------------------------------


def make_scope(tmp_path, project, scope):
    path = tmp_path / "projects" / project / scope
    path.mkdir(parents=True)
    return path


async def test_an_existing_scope_comes_back_with_its_worktree_and_the_live_sessions_in_it(env, tmp_path):
    path = make_scope(tmp_path, "factorio", "card-" + "00000001"[:8])
    card = env.federation_card(project="factorio", id="00000001" + "c0de" * 6)
    env.cx.rows = [
        {"job": "job00001", "name": "me", "state": "idle", "project": "factorio", "scope": "card-00000001"},
        {"job": "w9", "name": "writer", "state": "working", "project": "factorio", "scope": "card-00000001"},
        {"job": "w8", "name": "old", "state": "gone", "project": "factorio", "scope": "card-00000001"},
        {"job": "w7", "name": "elsewhere", "state": "idle", "project": "factorio", "scope": "other"},
    ]
    out = await env.call("claim_task", ref=ref(card))
    assert (out["project"], out["scope"], out["scope_source"]) == ("factorio", "card-00000001", "suggested")
    assert out["scope_exists"] is True and out["worktree"] == str(path)
    assert [s["job"] for s in out["sessions"]] == ["w9"]
    assert "Work in" in out["next"] and "already work there" in out["next"] and "scope create" not in out["next"]
    assert out["project_scopes"] == ["card-00000001"]


async def test_a_missing_scope_is_to_be_created(env, tmp_path):
    (tmp_path / "projects" / "factorio" / "dev").mkdir(parents=True)
    card = env.federation_card(project="factorio")
    out = await env.call("claim_task", ref=ref(card))
    assert out["scope_exists"] is False and out["worktree"] is None and out["sessions"] == []
    assert "scope create" in out["next"] and out["scope"] in out["next"]
    assert out["project_scopes"] == ["dev"]


async def test_a_scope_the_card_names_is_used_as_it_is(env, tmp_path):
    make_scope(tmp_path, "awm", "feat/intake")
    card = env.federation_card(project="awm", scope="feat/intake")
    out = await env.call("claim_task", ref=ref(card))
    assert (out["scope"], out["scope_source"], out["scope_exists"]) == ("feat/intake", "label", True)


async def test_a_card_with_no_project_asks_tony_where_to_work(env):
    card = env.federation_card()
    out = await env.call("claim_task", ref=ref(card))
    assert out["project"] is None and out["scope"] is None and "Ask Tony" in out["next"]


async def test_cx_being_down_does_not_fail_the_claim(env, tmp_path):
    make_scope(tmp_path, "awm", "dev")
    env.cx.unavailable = True
    card = env.federation_card(project="awm", scope="dev")
    out = await env.call("claim_task", ref=ref(card))
    assert out["ok"] and out["sessions"] is None and "cx is down" in out["sessions_error"]
