"""The task ledger and its in-place schema upgrade."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from awm.reporting import ledger
from awm.reporting.ledger import Ledger
from awm.reporting.store import Queue


pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "reporting.db")


def test_a_task_is_keyed_by_node_and_key_and_made_once(led):
    first, made = led.ensure("altair", "job00001", "bg", title="t", session_id="s1")
    again, made_again = led.ensure("altair", "job00001", "bg", title="other")
    assert (made, made_again) == (True, False)
    assert again["id"] == first["id"] and again["title"] == "t"
    assert led.ensure("capella", "job00001", "bg")[1] is True


def test_an_interactive_task_carries_its_pid(led):
    task, _ = led.ensure("altair", "321:9999", "interactive", pid=321)
    assert (task["kind"], task["pid"], task["state"]) == ("interactive", 321, ledger.PLANNED)


def test_session_ids_are_aliases_of_one_task(led):
    task, _ = led.ensure("altair", "job00001", "bg")
    led.alias(task["id"], "s1")
    led.alias(task["id"], "s2")
    led.alias(task["id"], "s1")
    assert led.aliases(task["id"]) == ["s1", "s2"]


def test_events_are_append_only_and_a_ref_lands_once(led):
    task, _ = led.ensure("altair", "j", "bg")
    assert led.event(task["id"], "journal", "first", ref="post1") is True
    assert led.event(task["id"], "journal", "first", ref="post1") is False
    led.event(task["id"], "moved", "active")
    assert [e["kind"] for e in led.events(task["id"])] == ["journal", "moved"]
    assert [e["detail"] for e in led.events(task["id"], "moved")] == ["active"]


def test_close_takes_a_task_out_of_the_open_list_and_a_resume_reopens_it(led):
    task, _ = led.ensure("altair", "j", "bg")
    other, _ = led.ensure("capella", "k", "bg")
    assert [t["id"] for t in led.open("altair")] == [task["id"]]
    led.close(task["id"])
    assert led.open("altair") == [] and [t["id"] for t in led.closed("altair")] == [task["id"]]
    assert [t["id"] for t in led.open()] == [other["id"]]
    again, made = led.ensure("altair", "j", "bg")
    assert made is False and again["state"] == ledger.PLANNED and again["closed"] is None
    assert "reopened" in [e["kind"] for e in led.events(task["id"])]


def test_close_refuses_an_open_state(led):
    task, _ = led.ensure("altair", "j", "bg")
    with pytest.raises(ValueError):
        led.close(task["id"], ledger.ACTIVE)


def items(*pairs):
    return [{"id": i, "subject": s, "status": st} for i, s, st in pairs]


def test_a_removed_todo_stays_with_its_removal_time_and_order(led):
    task, _ = led.ensure("altair", "j", "bg")
    t = task["id"]
    assert led.set_todos(t, items(("1", "a", "pending"), ("2", "b", "pending"), ("3", "c", "pending")))
    assert led.set_todos(t, items(("1", "a", "pending"), ("3", "c", "pending"))) is True
    assert led.set_todos(t, items(("3", "c", "pending"))) is True
    live, gone = led.todos(t)
    assert [i["item_id"] for i in live] == ["3"]
    assert [i["item_id"] for i in gone] == ["2", "1"]
    assert all(i["removed_at"] for i in gone)


def test_an_unchanged_list_changes_nothing_and_a_status_change_does(led):
    task, _ = led.ensure("altair", "j", "bg")
    t = task["id"]
    led.set_todos(t, items(("1", "a", "pending")))
    assert led.set_todos(t, items(("1", "a", "pending"))) is False
    assert led.set_todos(t, items(("1", "a", "completed"))) is True
    assert led.todos(t)[0][0]["status"] == "completed"


def test_an_empty_list_removes_every_open_item_and_a_deleted_status_counts_as_gone(led):
    task, _ = led.ensure("altair", "j", "bg")
    t = task["id"]
    led.set_todos(t, items(("1", "a", "pending"), ("2", "b", "pending")))
    led.set_todos(t, items(("1", "a", "deleted"), ("2", "b", "pending")))
    assert [i["item_id"] for i in led.todos(t)[1]] == ["1"]
    led.set_todos(t, [])
    live, gone = led.todos(t)
    assert live == [] and {i["item_id"] for i in gone} == {"1", "2"}


def test_a_removed_item_listed_again_comes_back(led):
    task, _ = led.ensure("altair", "j", "bg")
    t = task["id"]
    led.set_todos(t, items(("1", "a", "pending")))
    led.set_todos(t, [])
    led.set_todos(t, items(("1", "a", "pending")))
    live, gone = led.todos(t)
    assert [i["item_id"] for i in live] == ["1"] and gone == []


def test_update_refuses_a_column_it_does_not_know(led):
    task, _ = led.ensure("altair", "j", "bg")
    with pytest.raises(KeyError):
        led.update(task["id"], node="x")


# -- the door-database migration ---------------------------------------------

def make_v1(path: Path) -> None:
    from awm.reporting import store

    conn = sqlite3.connect(path)
    conn.executescript(store._QUEUE_SCHEMA + "CREATE TABLE schema_version (version INTEGER NOT NULL);"
                       "INSERT INTO schema_version VALUES (1);")
    conn.execute("INSERT INTO cards (card_id, kind, title, status, arrival, updated)"
                 " VALUES ('c1','request','t','done',1,1)")
    conn.execute("INSERT INTO sessions VALUES ('representative','abc',1)")
    conn.execute("INSERT INTO meta VALUES ('cursor','41')")
    conn.commit()
    conn.close()


def test_a_v1_database_migrates_in_place_to_the_task_schema(tmp_path):
    make_v1(tmp_path / "old.db")
    q = Queue(tmp_path / "old.db")
    assert q.get("c1")["status"] == "done" and q.cursor() == 41
    task, made = Ledger(tmp_path / "old.db").ensure("altair", "j", "bg")
    assert made and task["id"] == 1
