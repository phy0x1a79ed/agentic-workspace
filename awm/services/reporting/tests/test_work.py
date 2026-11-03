"""Task ids and the tasks that span sessions (sessions.md R57, R58)."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from awm.reporting import task_ids
from awm.reporting.task_verbs import TaskVerbs
from awm.reporting.work import Work

pytestmark = [pytest.mark.unit]

A = "11111111-2222-3333-4444-555555555555"
B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
C = "0f0f0f0f-0000-1111-2222-333333333333"
R9_A = "cc11111111222233334444555555555555"


@pytest.fixture
def work(tmp_path):
    return Work(tmp_path / "reporting.db")


def test_the_minter_matches_the_vector_shared_with_the_queue():
    raw = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff").bytes
    assert task_ids.mint_task_id(raw) == "ABEiM0RV"


def test_a_minted_id_is_8_url_safe_characters():
    for _ in range(200):
        tid = task_ids.mint_task_id()
        assert task_ids.is_task_id(tid) and "/" not in tid and "+" not in tid


def test_session_keys_take_the_r9_form_a_uuid_or_its_hex():
    assert task_ids.session_key(A) == R9_A
    assert task_ids.session_key(A.replace("-", "")) == R9_A
    assert task_ids.session_key(R9_A) == R9_A
    assert task_ids.claude_uuid(R9_A) == A
    with pytest.raises(ValueError):
        task_ids.session_key("job00001")


def test_open_makes_the_session_main(work):
    tid = work.task_open("redux naming", A, "mizar")
    assert work.task_for_session(A) == tid
    assert work.task_for_session(R9_A) == tid
    [s] = work.task_sessions(tid)
    assert (s["session_id"], s["role"], s["active"]) == (R9_A, "main", True)


def test_ensure_opens_once(work):
    tid, made = work.task_ensure("t", A)
    again, made_again = work.task_ensure("t", A)
    assert (made, made_again, again) == (True, False, tid)


def test_attach_folds_the_workers_own_task_into_the_coordinators(work):
    coord = work.task_open("coord", A)
    own = work.task_open("worker's own", B)
    out = work.task_attach(coord, B)
    assert out == {"task_id": coord, "main_session_id": R9_A, "folded_task_id": own}
    assert work.task_for_session(B) == coord
    assert {s["role"] for s in work.task_sessions(coord)} == {"main", "worker"}
    # The folded task resolves to the one it joined, and its row stays.
    assert work.task_get(own)["task_id"] == coord


def test_join_keeps_the_target_main_and_folds_its_sessions(work):
    running = work.task_open("running", A)
    mine = work.task_open("mine", B)
    work.task_attach(mine, C)
    out = work.task_join(running, B)
    assert out["folded_task_id"] == mine and out["main_session_id"] == R9_A
    sessions = {s["session_id"]: s["role"] for s in work.task_sessions(running)}
    assert sessions[R9_A] == "main"
    assert sessions[task_ids.session_key(B)] == "joined"
    assert sessions[task_ids.session_key(C)] == "worker"


def test_an_unknown_task_is_refused(work):
    with pytest.raises(ValueError):
        work.task_attach("ZZZZZZZZ", A)
    with pytest.raises(ValueError):
        work.task_sessions("not-an-id")


def test_session_end_marks_it_inactive(work):
    tid = work.task_open("t", A)
    assert work.task_session_end(tid, A) is True
    assert work.task_sessions(tid)[0]["active"] is False


def test_a_closed_ledger_row_reads_inactive(work):
    tid = work.task_open("t", A)
    row, _ = work.ensure("mizar", "job00001", "bg", session_id=A)
    work.close(row["id"])
    assert work.task_sessions(tid)[0]["active"] is False


def test_the_v9_ledger_upgrades_in_place(tmp_path):
    from awm.persistence import databases
    from awm.reporting import store

    path = tmp_path / "reporting.db"
    databases.init_db_at(path, store._QUEUE_SCHEMA + store._TASKS_SCHEMA, schema_version=9)
    Work(path).task_open("t", A)
    names = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert {"work", "work_sessions"} <= names


async def test_join_notifies_the_main_sessions_job(work):
    sent = []

    async def notify(job, text):
        sent.append((job, text))
        return True

    running = work.task_open("running", A)
    work.ensure("mizar", "job00001", "bg", session_id=A)
    verbs = TaskVerbs(work, "mizar", resolve=lambda pid: {"sessionId": B}, notify=notify)
    out = await verbs.call("task_join", {"task_id": running, "_caller_pid": 42})
    assert out["ok"] and out["main_notified"] is True
    assert sent and sent[0][0] == "job00001" and running in sent[0][1]


async def test_a_verb_without_a_session_says_so(work):
    verbs = TaskVerbs(work, "mizar", resolve=lambda pid: None)
    out = await verbs.call("task_for_session", {})
    assert out["ok"] is False and "session" in out["error"]


async def test_task_for_session_answers_none_for_a_stranger(work):
    verbs = TaskVerbs(work, "mizar", resolve=lambda pid: None)
    out = await verbs.call("task_for_session", {"session_id": B})
    assert out == {"ok": True, "session_id": task_ids.session_key(B), "task_id": None}
