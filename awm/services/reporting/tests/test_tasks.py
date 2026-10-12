"""The task verbs: claim, plan, approve, todos, blocked, review."""

from __future__ import annotations

import pytest

from awm.reporting.ledger import Ledger
from awm.reporting.swarmboard import SwarmBoard
from awm.reporting import tasks
from awm.reporting.tasks import Tasks

from stubs import FakeTrilium, make_board

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

BG = {"pid": 100, "kind": "bg", "jobId": "job00001", "sessionId": "sess-1", "cwd": "/nowhere", "procStart": "1"}
SCOPE_DIR = "projects/awm/feat/x"
TTY = {"pid": 200, "kind": "interactive", "sessionId": "sess-2", "cwd": "/nowhere", "procStart": "77"}


class Env:
    def __init__(self, tmp_path):
        self.tri = FakeTrilium()
        self.ids = make_board(self.tri)
        self.records = {100: dict(BG), 200: dict(TTY)}
        self.dms: list[str] = []
        self.dm_error: Exception | None = None
        self.job = {"intent": "Fix the thing\nmore", "respawnFlags": ["--model", "opus", "--effort=high"]}
        self.ledger = Ledger(tmp_path / "reporting.db")
        self.alive = {200: True}
        self.tasks = Tasks(self.ledger, SwarmBoard(self.tri), node="altair",
                           resolve=self.records.get, job_state=lambda job: self.job, dm=self._dm,
                           alive=lambda pid, start: self.alive.get(pid, False), fetch=self._fetch)
        self.posts: list[dict] = []
        self.fetches: list[dict] = []

    async def _fetch(self, service, fn, args, timeout=0):
        self.fetches.append(args)
        return {"posts": list(self.posts)}

    async def _dm(self, text):
        if self.dm_error:
            raise self.dm_error
        self.dms.append(text)

    async def call(self, verb, pid=100, **args):
        return await self.tasks.call(verb, {**args, "_caller_pid": pid})

    def task(self, key="job00001"):
        return self.ledger.find("altair", key)

    def body(self, task=None):
        return self.tri.notes[(task or self.task())["note_id"]]["content"]

    def column(self, note_id):
        cols = {v: k for k, v in self.ids.items()}
        return sorted(cols[p] for p in self.tri.parents(note_id) if p in cols)

    def tonys_note(self, title="Tony's task"):
        tonys = self.tri.add("Tasks", type="book")
        return self.tri.add(title, tonys, content="body"), tonys


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    return Env(tmp_path)


URL = "https://vault.example/#root/AbCdEfGhIjKl/{}"


async def test_a_call_without_a_session_is_refused(env):
    assert (await env.tasks.call("hook_plan", {"plan_text": "x"}))["ok"] is False
    assert (await env.call("hook_plan", pid=999, plan_text="x"))["ok"] is False
    assert env.ledger.open() == []


async def test_session_id_is_a_check_and_never_the_key(env):
    out = await env.call("hook_plan", session_id="someone-else", plan_text="x")
    assert out["ok"] is False and "does not match" in out["error"]
    assert (await env.call("hook_plan", session_id="sess-1", plan_text="x"))["ok"] is True


async def test_a_plan_from_an_untracked_session_makes_a_planned_card_with_the_intent(env):
    env.job = {"intent": "Fix the thing\nmore", "respawnFlags": ["--model", "opus", "--effort=high"]}
    out = await env.call("hook_plan", plan_path="/p/plan.md", plan_text="step one")
    task = env.task()
    assert out["state"] == "planned" and task["title"] == "Fix the thing" and task["owned"] == 1
    assert (task["model"], task["effort"]) == ("opus", "high")
    assert env.column(task["note_id"]) == ["Planned"]
    card = env.tri.notes[task["note_id"]]
    assert "step one" in card["content"] and "altair" in card["content"]
    assert card["type"] == "text" and card["labels"] == {"agentManaged": "", "readOnly": ""}
    assert env.tri.kids(task["note_id"]) == []


async def test_the_plan_title_is_used_when_the_session_has_no_intent(env):
    env.job = {}
    await env.call("hook_plan", plan_text="# Build the wrapper\n\nx")
    assert env.task()["title"] == "Build the wrapper"


async def test_a_second_plan_write_updates_the_same_record(env):
    await env.call("hook_plan", plan_text="v1")
    await env.call("hook_plan", plan_text="v2")
    card = env.task()["note_id"]
    assert env.tri.kids(card) == []
    assert "v2" in env.tri.notes[card]["content"] and "v1" not in env.tri.notes[card]["content"]
    assert len(env.ledger.open()) == 1


async def test_approving_moves_the_card_to_active(env):
    await env.call("hook_plan", plan_text="v1")
    out = await env.call("hook_plan_approved", plan_text="v1 final")
    task = env.task()
    assert out["state"] == "active" and env.column(task["note_id"]) == ["Active"]
    assert task["plan"] == "v1 final"


async def test_an_approval_with_no_earlier_plan_still_makes_an_active_card(env):
    await env.call("hook_plan_approved", plan_text="p")
    assert env.column(env.task()["note_id"]) == ["Active"]


async def test_the_prompt_hook_claims_a_task_from_tonys_board_and_keeps_his_copy(env):
    note, tonys = env.tonys_note()
    out = await env.call("hook_prompt", prompt=f"please claim this task {URL.format(note)} and go")
    task = env.task()
    assert out["ok"] and task["note_id"] == note and task["state"] == "planned" and task["title"] == "Tony's task"
    assert sorted(env.tri.parents(note)) == sorted([tonys, env.ids["Planned"]])
    assert [e["kind"] for e in env.ledger.events(task["id"])] == ["created", "claimed"]
    assert env.tri.notes[note]["content"] == "body"


async def test_claim_task_does_the_same_from_inside_a_session(env):
    note, tonys = env.tonys_note()
    out = await env.call("claim_task", url=URL.format(note))
    assert out["state"] == "planned" and env.column(note) == ["Planned"]
    assert tonys in env.tri.parents(note)


async def test_a_claim_of_a_card_already_on_the_swarm_board_does_not_clone(env):
    card = env.tri.add("queued", env.ids["Proposed"])
    await env.call("claim_task", url=URL.format(card))
    assert env.tri.parents(card) == [env.ids["Planned"]]
    assert "note_clone" not in [fn for fn, _ in env.tri.calls]


async def test_claiming_twice_is_idempotent_and_a_second_session_is_refused(env):
    note, _ = env.tonys_note()
    await env.call("claim_task", url=URL.format(note))
    await env.call("hook_plan_approved", plan_text="p")
    again = await env.call("claim_task", url=URL.format(note))
    assert again["ok"] and again["state"] == "active"
    other = await env.call("claim_task", pid=200, url=URL.format(note))
    assert other["ok"] is False and "already claimed" in other["error"]


async def test_a_prompt_without_claim_words_or_a_url_claims_nothing(env):
    note, _ = env.tonys_note()
    await env.call("hook_prompt", prompt=f"look at {URL.format(note)}")
    await env.call("hook_prompt", prompt="claim this")
    assert env.task() is None


async def test_claim_task_refuses_a_value_that_is_not_a_note_link(env):
    assert (await env.call("claim_task", url="not a link"))["ok"] is False


async def test_a_claim_refuses_when_the_board_is_not_made(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    env = Env(tmp_path)
    for n in list(env.tri.notes):
        if env.tri.notes[n]["labels"].get("swarmBoard") is not None:
            env.tri.notes[n]["labels"].clear()
    out = await env.call("hook_plan", plan_text="x")
    assert out["ok"] is False and "board_ensure" in out["error"]
    assert env.ledger.open() == []


def items(*pairs):
    return [{"id": i, "subject": s, "status": st} for i, s, st in pairs]


async def test_todos_rewrite_the_section_and_a_removed_item_stays_under_removed(env):
    await env.call("hook_plan", plan_text="p")
    await env.call("hook_todos", items=items(("1", "alpha", "pending"), ("2", "beta", "pending"), ("3", "gamma", "pending")))
    await env.call("hook_todos", items=items(("1", "alpha", "completed"), ("3", "gamma", "pending")))
    content = env.body()
    todo = content.split("<summary>Todo")[1].split("<summary>Journal")[0]
    assert '<input type="checkbox" disabled="disabled" checked="checked"><span class="todo-list__label__description">alpha' in todo
    assert '<input type="checkbox" disabled="disabled"><span class="todo-list__label__description">gamma' in todo
    assert "Todo — 1/2 done" in content and "beta" in content.split("<summary>Removed")[1]


async def test_the_last_task_deleted_removes_every_open_item(env):
    await env.call("hook_plan", plan_text="p")
    await env.call("hook_todos", items=items(("1", "alpha", "pending")))
    await env.call("hook_todos", items=[])
    live, gone = env.ledger.todos(env.task()["id"])
    assert live == [] and [i["subject"] for i in gone] == ["alpha"]


async def test_todos_from_an_untracked_session_make_no_card(env):
    out = await env.call("hook_todos", items=items(("1", "a", "pending")))
    assert out == {"ok": True, "tracked": False, "blocked": False} and env.task() is None


async def test_a_non_urgent_request_blocks_the_card_and_sends_nothing(env):
    await env.call("hook_plan_approved", plan_text="p")
    out = await env.call("request_human", reason="need the Globus login", urgent=False)
    assert out["blocked"] is True and env.column(env.task()["note_id"]) == ["Blocked"]
    assert env.dms == []
    rec = env.body()
    assert "need the Globus login" in rec


async def test_an_urgent_request_sends_one_message_with_the_link_and_the_attach_command(env, monkeypatch):
    monkeypatch.setenv("AWM_REPORTING_TRILIUM_URL", "https://vault.example/trilium")
    out = await env.call("request_human", reason="login expired", urgent=True)
    assert out["notified"] is True and len(env.dms) == 1
    text = env.dms[0]
    note = env.task()["note_id"]
    assert "login expired" in text and f"https://vault.example/trilium/#root/{note}" in text
    assert "claude attach job00001" in text and "altair" in text


async def test_a_failed_message_still_blocks_the_card(env):
    env.dm_error = RuntimeError("social down")
    out = await env.call("request_human", reason="x", urgent=True)
    assert out["ok"] and out["blocked"] and out["notified"] is False


async def test_a_request_needs_a_reason(env):
    assert (await env.call("request_human", reason=" "))["ok"] is False


async def test_the_next_prompt_moves_a_blocked_card_back_to_active(env):
    await env.call("request_human", reason="x", urgent=False)
    assert any(tasks.blocked_marker(sid).exists() for sid in env.ledger.aliases(env.task()["id"]))
    out = await env.call("hook_prompt", prompt="here is the login")
    task = env.task()
    assert out["blocked"] is False and task["state"] == "active" and task["reason"] == ""
    assert env.column(task["note_id"]) == ["Active"]
    assert (await env.call("hook_prompt", prompt="again"))["blocked"] is False


async def test_a_review_request_moves_the_card_to_pending_review(env):
    await env.call("hook_plan_approved", plan_text="p")
    out = await env.call("request_human", kind="review", summary="done; see data/out")
    assert out["state"] == "review" and env.column(env.task()["note_id"]) == ["Pending review"]
    assert (await env.call("request_human", kind="review", summary=""))["ok"] is False


async def test_an_interactive_session_is_keyed_by_pid_and_start_time(env):
    await env.call("hook_plan", pid=200, plan_text="# Mine\n\nx")
    task = env.task("200:77")
    assert task["kind"] == "interactive" and task["pid"] == 200 and task["title"] == "Mine"


async def test_a_new_conversation_under_the_same_job_is_an_alias(env):
    await env.call("hook_plan", plan_text="p")
    env.records[100]["sessionId"] = "sess-9"
    await env.call("hook_plan", plan_text="p2")
    task = env.task()
    assert task["session_id"] == "sess-9" and env.ledger.aliases(task["id"]) == ["sess-1", "sess-9"]
    assert len(env.ledger.open()) == 1


async def test_a_board_failure_keeps_the_change_and_marks_the_task_dirty(env):
    await env.call("hook_plan", plan_text="p")
    env.tri.fail = RuntimeError("trilium down")
    out = await env.call("hook_todos", items=items(("1", "alpha", "pending")))
    assert out["ok"] is True and env.task()["dirty"] == 1
    assert env.ledger.todos(env.task()["id"])[0][0]["subject"] == "alpha"
    env.tri.fail = None
    assert await env.tasks.sync(env.task()["id"]) is True and env.task()["dirty"] == 0


async def test_a_move_never_calls_note_move(env):
    note, _ = env.tonys_note()
    await env.call("claim_task", url=URL.format(note))
    await env.call("hook_plan_approved", plan_text="p")
    await env.call("request_human", kind="review", summary="s")
    assert "note_move" not in [fn for fn, _ in env.tri.calls]


# -- the watcher ---------------------------------------------------------------


def row(state, job="job00001"):
    return {"job": job, "state": state}


async def started(env):
    await env.call("hook_plan_approved", plan_text="p")
    return env.task()


async def test_a_stopped_job_closes_its_task_as_done_and_files_the_card_in_the_ledger(env):
    note, tonys = env.tonys_note()
    await env.call("claim_task", url=URL.format(note))
    await env.tasks.sweep([row("stopped")])
    task = env.task()
    assert task["state"] == "done" and task["closed"] and env.ledger.open() == []
    ledger_note = env.tri.kids(env.ids["Ledger"])[0]
    assert env.tri.notes[ledger_note]["title"] == "altair" and "Tony's task" in env.tri.notes[ledger_note]["content"]
    assert set(env.tri.notes[ledger_note]["labels"]) == {"agentManaged", "readOnly"}
    assert sorted(env.tri.parents(note)) == sorted([tonys, ledger_note])
    assert env.tri.kids(note) and note in env.tri.notes


async def test_a_job_that_died_without_a_stop_stays_on_the_board_flagged_and_resumes(env):
    task = await started(env)
    await env.tasks.sweep([row("gone")])
    assert env.task()["ended"] == 1 and env.column(task["note_id"]) == ["Active"]
    assert "died without a stop" in env.body(task)
    await env.tasks.sweep([row("gone")])
    assert [e["kind"] for e in env.ledger.events(task["id"])].count("ended") == 1
    await env.tasks.sweep([row("working")])
    assert env.task()["ended"] == 0 and env.task()["state"] == "active"


async def test_an_idle_session_waiting_on_slurm_stays_active(env):
    await started(env)
    for state in ("idle", "working", "blocked", "done"):
        await env.tasks.sweep([row(state)])
    assert env.task()["state"] == "active"


async def test_a_job_with_no_row_closes_only_on_the_second_sweep(env):
    await started(env)
    await env.tasks.sweep([])
    assert env.task()["state"] == "active"
    await env.tasks.sweep([row("working")])
    await env.tasks.sweep([])
    assert env.task()["state"] == "active"
    await env.tasks.sweep([])
    assert env.task()["state"] == "done"


async def test_an_interactive_session_that_exits_closes_its_task(env):
    await env.call("hook_plan", pid=200, plan_text="# Mine\n\nx")
    await env.tasks.sweep([])
    assert env.task("200:77")["state"] == "planned"
    env.alive[200] = False
    await env.tasks.sweep([])
    assert env.task("200:77")["state"] == "done"


async def test_a_card_tony_removes_from_the_board_is_recorded_as_cancelled(env):
    task = await started(env)
    env.tri.notes[task["note_id"]]["parents"] = ["root"]
    await env.tasks.sweep([row("working")])
    assert env.task()["state"] != "cancelled"
    await env.tasks.sweep([row("working")])
    assert env.task()["state"] == "cancelled" and env.ledger.open() == []
    content = env.tri.notes[env.tri.kids(env.ids["Ledger"])[0]]["content"]
    assert "cancelled" in content


async def test_a_column_drag_by_tony_is_an_event_and_is_not_moved_back(env):
    task = await started(env)
    env.tri.notes[task["note_id"]]["parents"] = [env.ids["Pending review"]]
    await env.tasks.sweep([row("working")])
    after = env.task()
    assert after["state"] == "review" and env.column(task["note_id"]) == ["Pending review"]
    assert ("dragged", "Active -> Pending review") in [(e["kind"], e["detail"]) for e in env.ledger.events(task["id"])]
    await env.tasks.sweep([row("working")])
    assert [e["kind"] for e in env.ledger.events(task["id"])].count("dragged") == 1


async def test_a_sweep_retries_what_the_board_refused(env):
    task = await started(env)
    env.tri.fail = RuntimeError("down")
    await env.call("hook_todos", items=items(("1", "alpha", "pending")))
    await env.tasks.sweep([row("working")])
    assert env.task()["dirty"] == 1 and env.task()["state"] == "active"
    env.tri.fail = None
    await env.tasks.sweep([row("working")])
    assert env.task()["dirty"] == 0
    assert "alpha" in env.body(task)


async def test_a_close_the_board_refused_is_filed_once_the_board_is_back(env):
    task = await started(env)
    env.tri.fail = RuntimeError("down")
    await env.tasks.sweep([row("stopped")])
    assert env.task()["state"] == "done" and env.task()["dirty"] == 1
    env.tri.fail = None
    await env.tasks.sweep([])
    assert env.task()["dirty"] == 0 and env.column(task["note_id"]) == []


async def test_a_blocked_urgent_task_gets_one_reminder_after_the_delay(env, monkeypatch):
    monkeypatch.setenv("AWM_REPORTING_REMIND_S", "60")
    await env.call("request_human", reason="login", urgent=True)
    await env.tasks.sweep([row("working")])
    assert len(env.dms) == 1
    env.ledger.update(env.task()["id"], blocked_at=env.task()["blocked_at"] - 120)
    await env.tasks.sweep([row("working")])
    await env.tasks.sweep([row("working")])
    assert len(env.dms) == 2 and env.dms[1].startswith("Still waiting")


async def test_no_reminder_when_the_card_was_unblocked_or_not_urgent(env, monkeypatch):
    monkeypatch.setenv("AWM_REPORTING_REMIND_S", "60")
    await env.call("request_human", reason="a", urgent=False)
    env.ledger.update(env.task()["id"], blocked_at=1.0)
    await env.tasks.sweep([row("working")])
    await env.call("request_human", reason="b", urgent=True)
    await env.call("hook_prompt", prompt="ok")
    env.ledger.update(env.task()["id"], blocked_at=1.0)
    await env.tasks.sweep([row("working")])
    assert len(env.dms) == 1


# -- journals ------------------------------------------------------------------


def post(pid="p1", scope="feat/x", ts=None, **over):
    import time
    from datetime import datetime, timezone

    iso = datetime.fromtimestamp(ts or time.time() + 5, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": pid, "project": "awm", "scope": scope, "kind": "journal", "body": "did the thing",
            "ts": iso, "meta": {}, **over}


async def in_scope(env, tmp_path):
    env.records[100]["cwd"] = str(tmp_path / SCOPE_DIR)
    (tmp_path / SCOPE_DIR).mkdir(parents=True)
    await env.call("hook_plan_approved", plan_text="p")
    assert (env.task()["project"], env.task()["scope"]) == ("awm", "feat/x")


def journal_of(env):
    return env.body().split("<summary>Journal")[1].split("<summary>Removed")[0]


async def test_a_journal_post_from_the_tasks_scope_lands_in_its_journal_section(env, tmp_path):
    await in_scope(env, tmp_path)
    await env.tasks.on_post({"project": "awm", "scope": "feat/x", "post": post(meta={"title": "Session 1"})})
    assert "Session 1" in journal_of(env) and "did the thing" in journal_of(env)


async def test_posts_from_other_scopes_other_kinds_or_before_the_task_are_ignored(env, tmp_path):
    await in_scope(env, tmp_path)
    for p in (post("a", scope="feat/y"), post("b", kind="message"), post("c", ts=1.0), post("d", body=" ")):
        await env.tasks.on_post({"post": p})
    assert "<p>(none)</p>" in journal_of(env) and "<h4>" not in journal_of(env)


async def test_the_same_post_twice_is_appended_once(env, tmp_path):
    await in_scope(env, tmp_path)
    for _ in range(2):
        await env.tasks.on_post({"post": post()})
    assert journal_of(env).count("did the thing") == 1


async def test_the_catch_up_finds_a_post_the_subscription_missed_and_runs_on_a_timer(env, tmp_path):
    await in_scope(env, tmp_path)
    env.posts = [post("old", ts=1.0), post("new")]
    await env.tasks.sweep([row("working")])
    assert journal_of(env).count("did the thing") == 1
    assert env.fetches == [{"project": "awm", "scope": "feat/x", "kind": "journal", "limit": 50, "order": "desc"}]
    await env.tasks.sweep([row("working")])
    assert len(env.fetches) == 1


async def test_a_post_for_a_closed_task_is_not_appended(env, tmp_path):
    await in_scope(env, tmp_path)
    await env.tasks.sweep([row("stopped")])
    await env.tasks.on_post({"post": post()})
    assert env.ledger.events(env.task()["id"], "journal") == []
