"""The card body, its title, the clear hook and the one-off conversion."""

from __future__ import annotations

import re

import pytest

from awm.persistence import databases
from awm.reporting import record, store
from awm.reporting.ledger import Ledger

from test_tasks import BG, Env, URL, items, row

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_PROJECTS", str(tmp_path / "projects"))
    return Env(tmp_path)


def task(**over):
    base = {"node": "altair", "key": "job1", "kind": "bg", "state": "active", "started": 1e9, "closed": None,
            "plan": "", "reason": "", "ended": 0, "project": "awm", "scope": "feat/x", "session_id": "s1",
            "model": None, "effort": None}
    return {**base, **over}


def render(plan="", live=(), removed=(), events=(), **over):
    return record.render_record(task(plan=plan, **over), list(live), list(removed), list(events))


def todo(status, subject="x", item_id="1"):
    return {"item_id": item_id, "subject": subject, "status": status}


def test_the_plans_first_h1_is_dropped_and_the_rest_starts_at_h3():
    html = render("# Title\n\nintro\n\n## A\n\n### B\n\n#### C\n\n# Second")
    plan = html.split("<summary>Plan</summary>")[1].split("</details>")[0]
    assert "Title" not in plan and "<h3>A</h3>" in plan and "<h4>B</h4>" in plan and "<h5>C</h5>" in plan
    assert "<h3>Second</h3>" in plan and "<h1>" not in html and "<h2>" not in html


def test_html_in_a_plan_or_a_subject_is_escaped():
    html = render("## A\n\n<script>alert(1)</script>", live=[todo("pending", "<b>x</b> & y")])
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in html


def test_the_body_is_a_status_block_then_four_closed_collapsibles_split_by_rules():
    html = render("## A", live=[todo("completed"), todo("in_progress", item_id="2"), todo("pending", item_id="3")],
                  removed=[{"subject": "gone", "removed_at": 1e9}],
                  events=[{"kind": "journal", "at": 1e9, "detail": "Session 1\n\nwork"}], state="blocked", reason="login")
    assert html.count('<details class="trilium-collapsible">') == 4 and "open" not in "".join(re.findall(r"<details[^>]*>", html))
    summaries = re.findall(r"<summary>(.*?)</summary>", html)
    assert summaries == ["Plan", "Todo — 1/3 done, 1 active", "Journal — 1 entry", "Removed — 1"]
    assert html.startswith("<ul>") and html.count("<hr>") == 4
    status = html.split("<hr>")[0]
    for text in ("State:</strong> Blocked", "Scope:</strong> awm/feat/x", "Peer:</strong> altair",
                 "Job:</strong> job1", "Waiting on:</strong> login", "Started:", ):
        assert text in status


def test_the_journal_entry_is_an_h4_and_its_body_starts_at_h5():
    html = render(events=[{"kind": "journal", "at": 1e9, "detail": "# Heading\n\ntext\n\n### Deep"}])
    journal = html.split("<summary>Journal — 1 entry</summary>")[1].split("</details>")[0]
    assert "<h4>2001-09-09 01:46Z</h4>" in journal and "<h5>Heading</h5>" in journal and "<h6>Deep</h6>" in journal


def test_a_pending_item_is_an_empty_box_a_completed_one_ticked_and_an_active_one_bold_with_a_note():
    html = render(live=[todo("pending", "p", "1"), todo("completed", "c", "2"), todo("in_progress", "a", "3")])
    lis = re.findall(r"<li><label.*?</li>", html)
    box = '<input type="checkbox" disabled="disabled"%s><span class="todo-list__label__description">%s</span>'
    assert lis == [f'<li><label class="todo-list__label">{box % ("", "p")}</label></li>',
                   f'<li><label class="todo-list__label">{box % (" checked=\"checked\"", "c")}</label></li>',
                   f'<li><label class="todo-list__label">{box % ("", "<strong>a</strong> (in progress)")}</label></li>']
    assert "[~]" not in html and '<ul class="todo-list">' in html


def test_empty_sections_say_none_and_removed_is_left_out_until_something_is_removed():
    html = render()
    assert html.count("<p>(none)</p>") == 3 and "Removed" not in html
    assert html.count("<hr>") == 3 and html.count("<details") == 3
    assert "Todo — none" in html and "Journal — 0 entries" in html


def test_cut_at_word_stops_at_a_boundary_within_the_limit():
    assert record.cut_at_word("a" * 10, 80) == "a" * 10
    text = "word " * 30
    cut = record.cut_at_word(text, 80)
    assert len(cut) <= 80 and cut == ("word " * 16).strip() and not cut.endswith(" ")
    assert record.cut_at_word("x" * 200, 80) == "x" * 80
    assert record.cut_at_word("one two", 4) == "one"


async def test_the_title_prefers_the_plans_heading_then_the_auto_name_then_the_intent(env):
    env.job = {"intent": "Fix the thing please", "name": "auto name", "nameSource": "auto"}
    await env.call("hook_prompt", prompt="hi")
    await env.call("hook_plan_approved", plan_text="body only")
    assert env.task()["title"] == "auto name"
    await env.call("hook_plan", plan_text="# Build the wrapper\n\nx")
    assert env.task()["title"] == "Build the wrapper"
    assert env.tri.notes[env.task()["note_id"]]["title"] == "Build the wrapper"


@pytest.mark.parametrize("job,want", [
    ({"intent": "Fix the thing\nmore", "name": "claimed otter", "nameSource": "auto"}, "Fix the thing"),
    ({"intent": "Fix the thing", "name": "claimed mine", "nameSource": "user"}, "Fix the thing"),
    ({"intent": "w " * 60, "name": "", "nameSource": "auto"}, ("w " * 40).strip()),
    ({}, "session job00001"),
])
async def test_the_title_falls_back_to_the_intent_cut_at_80_characters(env, job, want):
    env.job = job
    await env.call("hook_plan_approved", plan_text="p")
    assert env.task()["title"] == want[:80].rstrip()


async def test_a_user_given_name_titles_the_card(env):
    env.job = {"intent": "Fix the thing", "name": "msm writing", "nameSource": "user"}
    await env.call("hook_plan_approved", plan_text="p")
    assert env.task()["title"] == "msm writing"
    env.job = {"intent": "Fix the thing", "name": "msm writing", "nameSource": "other"}
    await env.call("hook_todos", items=items(("1", "a", "pending")))
    assert env.task()["title"] == "Fix the thing"


async def test_sync_retitles_an_owned_card_when_a_better_name_appears_and_leaves_a_claimed_one(env):
    env.job = {"intent": "first words"}
    await env.call("hook_plan_approved", plan_text="p")
    card = env.task()["note_id"]
    assert env.tri.notes[card]["title"] == "first words"
    env.job = {"intent": "first words", "name": "Named later", "nameSource": "auto"}
    await env.call("hook_todos", items=items(("1", "a", "pending")))
    assert env.tri.notes[card]["title"] == "Named later" and env.task()["title"] == "Named later"

    note, _ = env.tonys_note("Tony's title")
    await env.call("claim_task", pid=200, url=URL.format(note))
    await env.call("hook_plan", pid=200, plan_text="# Other\n\nx")
    assert env.tri.notes[note]["title"] == "Tony's title"
    assert env.ledger.find("altair", "200:77")["owned"] == 0


async def test_a_claimed_card_keeps_a_record_child_and_its_own_body(env):
    note, _ = env.tonys_note()
    await env.call("claim_task", url=URL.format(note))
    assert env.task()["owned"] == 0
    (kid,) = env.tri.kids(note)
    rec = env.tri.notes[kid]
    assert rec["title"] == "Record" and rec["type"] == "text" and "<details" in rec["content"]
    assert env.tri.notes[note]["content"] == "body"


async def test_a_card_claimed_after_an_own_card_stops_being_owned(env):
    await env.call("hook_plan_approved", plan_text="p")
    assert env.task()["owned"] == 1
    note, _ = env.tonys_note()
    await env.call("claim_task", url=URL.format(note))
    assert env.task()["owned"] == 0 and env.task()["note_id"] == note


async def test_a_clear_rekeys_the_open_task_and_the_next_plan_opens_a_new_card(env):
    await env.call("hook_plan_approved", plan_text="# First\n\nx")
    old = env.task()
    out = await env.call("hook_session_clear")
    assert out["ok"] and env.task() is None
    assert env.ledger.find("altair", "job00001/sess-1")["id"] == old["id"]
    env.records[100]["sessionId"] = "sess-new"
    await env.call("hook_plan", plan_text="# Second\n\nx")
    new = env.task()
    assert new["id"] != old["id"] and new["note_id"] != old["note_id"] and new["title"] == "Second"
    assert env.tri.notes[old["note_id"]]["title"] == "First"
    assert "Second" not in env.tri.notes[old["note_id"]]["content"]


async def test_a_clear_with_no_open_task_does_nothing(env):
    assert await env.call("hook_session_clear") == {"ok": True, "tracked": False, "blocked": False}
    assert env.ledger.open() == []


async def test_a_rekeyed_key_is_never_found_by_prefix(env):
    await env.call("hook_plan_approved", plan_text="p")
    await env.call("hook_session_clear")
    assert env.ledger.find("altair", "job00001") is None
    assert env.task("job00001/sess-1")["state"] == "active"
    assert env.ledger.find("altair", "job00001/") is None and env.ledger.find("altair", "job00001/sess") is None
    again = await env.call("hook_plan", plan_text="q")
    assert again["ok"] and env.task()["key"] == "job00001" and len(env.ledger.open()) == 2


async def test_the_sweep_closes_the_rekeyed_task_and_files_its_card(env):
    await env.call("hook_plan_approved", plan_text="p")
    old = env.task()
    await env.call("hook_session_clear")
    await env.call("hook_plan", plan_text="# New\n\nx")
    for _ in range(2):
        await env.tasks.sweep([row("working")])
    assert env.ledger.get(old["id"])["state"] == "done"
    assert env.task()["state"] == "planned"


async def test_a_clear_title_survives_for_the_filed_task(env):
    env.job = {"intent": "Original ask"}
    await env.call("hook_plan_approved", plan_text="p")
    await env.call("hook_session_clear")
    env.job = {}
    await env.tasks.sweep([row("working")])
    old = env.ledger.find("altair", "job00001/sess-1")
    assert env.tri.notes[old["note_id"]]["title"] == "Original ask"


async def test_request_human_picks_its_kind_from_the_arguments_when_none_is_given(env):
    out = await env.call("request_human", reason="need login")
    assert out["state"] == "blocked"
    out = await env.call("request_human", summary="all done")
    assert out["state"] == "review"
    assert (await env.call("request_human", kind="review", reason="done via reason"))["state"] == "review"
    assert (await env.call("request_human", kind="blocked", reason="again", urgent=False))["state"] == "blocked"
    bad = await env.call("request_human", kind="sideways", reason="x")
    assert bad["ok"] is False and "blocked or review" in bad["error"]
    assert (await env.call("request_human", kind="review"))["ok"] is False


async def test_convert_rerenders_open_cards_drops_owned_records_and_stamps_ops(env):
    await env.call("hook_plan_approved", plan_text="# Title\n\n## A")
    owned = env.task()
    stale = env.tri.add("Record", owned["note_id"], type="code", content="md", labels={"agentManaged": ""})
    env.tri.notes[owned["note_id"]].update(content="", title="whole prompt")
    note, _ = env.tonys_note()
    await env.call("claim_task", pid=200, url=URL.format(note))
    claimed_rec = env.tri.kids(note)[0]
    env.tri.notes[claimed_rec].update(type="code", content="md")
    ops = (await env.tasks.board.ensure())["ops"]
    legacy = env.tri.add("password - altair", ops, type="code", content="## Login\n\nx", labels={"agentManaged": ""})

    before = {n: v.get("content") for n, v in env.tri.notes.items()}
    dry = await env.tasks.convert(dry_run=True)
    assert dry["dry_run"] is True and len(dry["cards"]) == 2 and dry["ops"][0]["note_id"] == legacy
    assert env.tri.notes[owned["note_id"]]["content"] == "" and stale in env.tri.notes
    assert "changed" not in env.tri.notes[legacy]["labels"]
    assert {n: v.get("content") for n, v in env.tri.notes.items()} == before

    assert (await env.tasks.convert())["dry_run"] is True
    assert env.tri.notes[owned["note_id"]]["content"] == "" and stale in env.tri.notes

    done = await env.tasks.convert(dry_run=False)
    assert done["dry_run"] is False
    card = env.tri.notes[owned["note_id"]]
    assert "<details" in card["content"] and card["title"] == "Title"
    assert card["labels"] == {"agentManaged": "", "readOnly": ""} and stale not in env.tri.notes
    assert env.tri.notes[claimed_rec]["type"] == "text" and "<details" in env.tri.notes[claimed_rec]["content"]
    assert env.tri.notes[note]["content"] == "body" and env.tri.notes[note]["title"] == "Tony's task"
    assert env.tri.notes[legacy]["labels"]["changed"] and env.tri.notes[legacy]["content"].startswith("Changed: ")
    again = await env.tasks.convert(dry_run=False)
    assert again["ops"] == []


def test_the_task_schema_gains_owned_and_old_rows_keep_their_origin(tmp_path):
    path = tmp_path / "old.db"
    databases.init_db_at(path, store._QUEUE_SCHEMA_V3 + store._TASKS_SCHEMA_V2, schema_version=2)
    conn = databases.get_connection_at(path)
    now = 1.0
    for key in ("mine", "taken"):
        conn.execute("INSERT INTO tasks (node, key, kind, state, started, updated) VALUES ('n', ?, 'bg', 'active', ?, ?)",
                     (key, now, now))
    conn.execute("INSERT INTO task_events (task_id, at, kind) VALUES (2, 1.0, 'claimed')")
    conn.commit()
    conn.close()
    ledger = Ledger(path)
    assert ledger.find("n", "mine")["owned"] == 1 and ledger.find("n", "taken")["owned"] == 0
