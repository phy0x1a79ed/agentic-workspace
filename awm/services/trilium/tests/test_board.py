"""The board writer, the layout migration and the fence, against a stub of alcor and a fake vault
(queue.md R61-R66, fleet.md R106 item 2)."""

from __future__ import annotations

import json

import pytest

from awm.trilium.board import ids, install, migrate, render
from awm.trilium.board.fence import Fence, Refused
from awm.trilium.board.writer import Writer

from .board_stubs import Alcor, card, make_vault, remote

pytestmark = [pytest.mark.unit]


@pytest.fixture
def world(monkeypatch):
    fake, fence = make_vault(monkeypatch)
    alcor = Alcor()
    install.ensure_tasks(fence)
    return fake, fence, alcor, Writer(fence, remote(alcor))


def folder_of(fake, task_id):
    return fake.notes[ids.card_note_id(task_id)]["parents"]


# -- sections -------------------------------------------------------------------------------------------

def test_the_board_has_four_columns_and_no_proposed_or_triage(world):
    fake, fence, *_ = world
    titles = [fake.notes[c]["title"] for c in fake.notes[ids.TASKS_ID]["children"]]
    assert titles == ["Needs you", "Working", "Stale", "History"]


@pytest.mark.parametrize("section,column", [
    ("needs_you", "awmSwarmColNeedsYou"), ("working", "awmSwarmColWorking"),
    ("stale", "awmSwarmColStale"), ("history", "awmSwarmColHistory"),
    ("done", "awmSwarmColHistory"), ("proposed", "awmSwarmColHistory")])
def test_a_card_sits_in_its_sections_folder(world, section, column):
    fake, fence, alcor, w = world
    w.upsert(card("a", section))
    assert folder_of(fake, "a") == [column]
    assert fake.labels(ids.card_note_id("a"))["section"] == render.section_of({"section": section})


def test_a_card_moves_when_its_section_changes_and_keeps_its_note_id(world):
    fake, fence, alcor, w = world
    w.upsert(card("a", "working"))
    w.upsert(card("a", "stale"))
    w.upsert(card("a", "history", done_at=3000))
    assert folder_of(fake, "a") == ["awmSwarmColHistory"]
    labels = fake.labels(ids.card_note_id("a"))
    assert labels["historyAt"] == "3000" and "doneAt" not in labels


def test_chips_come_from_status_and_node_labels(world):
    fake, fence, alcor, w = world
    w.upsert(card("a", "needs_you", status="blocked", peer="alcor"))
    labels = fake.labels(ids.card_note_id("a"))
    assert (labels["status"], labels["peer"]) == ("blocked", "alcor")


# -- no null anywhere ---------------------------------------------------------------------------------------

def test_no_card_body_or_label_says_null(world):
    fake, fence, alcor, w = world
    w.upsert(card("a", "working", status=None, peer=None, meta={"reason": None, "todos": None},
                  sessions=[{"session_id": "s1", "name": None, "role": None, "active": False}]))
    nid = ids.card_note_id("a")
    body = fake.notes[nid]["content"]
    assert "null" not in body.lower() and "none" not in body.lower()
    assert not [v for v in fake.labels(nid).values() if v in ("None", "null")]


def test_a_proposed_note_keeps_its_text_and_a_null_body_is_rebuilt(world):
    fake, fence, alcor, w = world
    w.upsert(card("n", "history", kind="note", body="Tony's own words"))
    nid = ids.card_note_id("n")
    assert fake.notes[nid]["content"] == "Tony's own words"
    w.upsert(card("n", "history", kind="note", body="ignored on update"))
    assert fake.notes[nid]["content"] == "Tony's own words"
    fake.notes[nid]["content"] = "null"
    w.upsert(card("n", "history", kind="note", body=None))
    assert "null" not in fake.notes[nid]["content"]


# -- the record the panel reads --------------------------------------------------------------------------------

def test_the_record_carries_sessions_with_claude_links_and_approvals_pending_first(world):
    fake, fence, alcor, w = world
    w.upsert(card("a", "needs_you", sessions=[
        {"session_id": "s1", "name": "worker", "role": "main", "active": True, "node": "mizar",
         "rc_url": "https://claude.ai/code/session_x"}],
        approvals=[{"request_id": "r1", "state": "approved", "summary": "old", "requested_at": 1},
                   {"request_id": "r2", "state": "pending", "summary": "now", "requested_at": 2},
                   {"request_id": "r3", "state": "rejected", "summary": "mid", "requested_at": 3}]))
    rec = json.loads(fake.notes[ids.record_note_id("a")]["content"])
    assert rec["sessions"][0]["rc_url"].startswith("https://claude.ai/")
    assert [a["request_id"] for a in rec["approvals"]] == ["r2", "r3", "r1"]
    assert fake.labels(ids.card_note_id("a"))["pendingApprovals"] == "1"


def test_a_folded_card_loses_its_note(world):
    fake, fence, alcor, w = world
    w.upsert(card("a"))
    w.upsert(card("a", folded_into="b"))
    assert ids.card_note_id("a") not in fake.notes


# -- the note a card was started from ---------------------------------------------------------------------------

@pytest.fixture
def source(world):
    fake, fence, alcor, w = world
    fence.c.create_note(parent_note_id="root", title="Paper", type="text", content="body",
                        note_id="srcNote")
    return world


def test_a_card_from_a_note_holds_a_clone_and_the_source_holds_a_marker(source):
    fake, fence, alcor, w = source
    w.upsert(card("a", "needs_you", source_note="srcNote"))
    nid = ids.card_note_id("a")
    assert nid in fake.notes["srcNote"]["parents"] and "root" in fake.notes["srcNote"]["parents"]
    marker = ids.marker_note_id("a")
    assert fake.notes[marker]["parents"] == ["srcNote"]
    assert fake.labels(marker)["awmMarker"] == "a"
    assert nid in fake.notes[marker]["content"]
    rel = [x for x in fake.attributes.values() if x["noteId"] == marker and x["type"] == "relation"]
    assert rel[0]["value"] == nid
    before = len(fake.notes)
    w.upsert(card("a", "working", source_note="srcNote"))
    assert len(fake.notes) == before


def test_the_marker_goes_when_the_task_ends_and_the_source_survives(source):
    fake, fence, alcor, w = source
    w.upsert(card("a", "working", source_note="srcNote"))
    w.upsert(card("a", "history", source_note="srcNote", done_at=5))
    assert ids.marker_note_id("a") not in fake.notes
    assert "srcNote" in fake.notes and fake.notes["srcNote"]["content"] == "body"
    assert fence.note("srcNote")["parentNoteIds"].count("root") == 1


def test_a_missing_source_note_links_nothing(world):
    fake, fence, alcor, w = world
    w.upsert(card("a", "working", source_note="gone"))
    assert ids.marker_note_id("a") not in fake.notes


def test_removing_a_clone_removes_only_the_branch(source):
    fake, fence, alcor, w = source
    w.upsert(card("a", "working", source_note="srcNote"))
    nid = ids.card_note_id("a")
    assert fence.unclone("srcNote", nid) is True
    assert "srcNote" in fake.notes and fake.notes["srcNote"]["parents"] == ["root"]
    assert fence.unclone("srcNote", nid) is False


def test_a_clone_is_never_removed_when_it_is_the_notes_only_branch(world):
    fake, fence, alcor, w = world
    w.upsert(card("a"))
    nid = ids.card_note_id("a")
    fence.c.create_note(parent_note_id=nid, title="only", type="text", content="x", note_id="solo")
    with pytest.raises(Refused):
        fence.unclone("solo", nid)
    assert "solo" in fake.notes


# -- the fence ----------------------------------------------------------------------------------------------------------

def test_a_write_outside_the_roots_is_refused(world):
    fake, fence, alcor, w = world
    fence.c.create_note(parent_note_id="root", title="mine", type="text", content="", note_id="mine")
    for call in (lambda: fence.create("mine", "x"), lambda: fence.update("mine", content="y"),
                 lambda: fence.delete("mine"), lambda: fence.move("mine", ids.TASKS_ID),
                 lambda: fence.relation("mine", "r", ids.TASKS_ID),
                 lambda: fence.update("mine", labels={"a": "b"})):
        with pytest.raises(Refused):
            call()
    assert fake.notes["mine"]["content"] == ""


def test_reads_go_anywhere_and_a_root_may_be_made_anywhere(world):
    fake, fence, alcor, w = world
    assert fence.note("srcNote") is None and fence.note(ids.BOOK_ID)["title"] == "AWM"
    assert fence.content(ids.BOOK_ID) == ""
    fence.create(ids.BOOK_ID, "Swarm board", "render", note_id=ids.ROOT_ID)
    assert ids.ROOT_ID in fake.notes


def test_a_root_never_moves_or_goes(world):
    fake, fence, alcor, w = world
    with pytest.raises(Refused):
        fence.move(ids.TASKS_ID, ids.BOOK_ID)
    with pytest.raises(Refused):
        fence.delete(ids.TASKS_ID)


def test_the_roots_are_this_hosts_setting(world, monkeypatch):
    fake, fence, alcor, w = world
    monkeypatch.setenv("AWM_TRILIUM_WRITER_ROOTS", "root")
    with pytest.raises(Refused, match="no writer subtree"):
        Fence()


def test_only_a_labelled_marker_is_deleted(world):
    fake, fence, alcor, w = world
    fence.c.create_note(parent_note_id="root", title="imposter", type="text", content="",
                        note_id=ids.marker_note_id("zz"))
    with pytest.raises(Refused):
        fence.drop_marker("zz")
    assert ids.marker_note_id("zz") in fake.notes


# -- the drain --------------------------------------------------------------------------------------------------------------

def test_drain_applies_each_change_once_and_keeps_its_mark(world):
    fake, fence, alcor, w = world
    alcor.emit(card("a", "working"))
    alcor.emit(card("b", "needs_you"))
    alcor.emit(card("a", "stale"))
    assert w.drain() == 2
    assert folder_of(fake, "a") == ["awmSwarmColStale"]
    assert w.mark() == 3
    assert w.drain() == 0
    assert alcor.calls[-1][2]["after"] == 3


def test_drain_pages_until_alcor_says_there_is_no_more(world, monkeypatch):
    fake, fence, alcor, w = world
    monkeypatch.setattr("awm.trilium.board.writer.BATCH", 2)
    for i in range(5):
        alcor.emit(card(f"c{i}"))
    assert w.drain() == 5
    assert w.mark() == 5


def test_a_replay_changes_nothing_in_the_vault(world):
    fake, fence, alcor, w = world
    alcor.emit(card("a", "working", sessions=[{"session_id": "s", "name": "n", "active": True}]))
    w.drain()
    fake.calls.clear()
    w.upsert(alcor.events[0][1])
    writes = [c for c in fake.calls if c[0] in ("PATCH", "PUT", "POST", "DELETE")]
    assert writes == []


def test_one_bad_card_does_not_stop_the_rest(world):
    fake, fence, alcor, w = world
    alcor.emit(card("a"))
    alcor.emit({"task_id": "b", "section": "working"})  # no title: still written
    alcor.emit({"section": "working"})                    # no task id: skipped
    assert w.drain() == 2
    assert ids.card_note_id("b") in fake.notes


# -- the layout migration ---------------------------------------------------------------------------------------------------

def legacy_vault(fence):
    c = fence.c
    for sect, nid in (("proposed", "awmSwarmColProposed"), ("done", "awmSwarmColDone"),
                      ("triage", "awmSwarmColTriage")):
        c.create_note(parent_note_id=ids.TASKS_ID, title=sect, type="book", content="", note_id=nid)
    for tid, folder, sect, kind in (("p1", "awmSwarmColProposed", "proposed", "note"),
                                    ("d1", "awmSwarmColDone", "done", "task"),
                                    ("w1", "awmSwarmColWorking", "working", "task")):
        nid = ids.card_note_id(tid)
        c.create_note(parent_note_id=folder, title=tid, type="text",
                      content="Tony wrote this" if kind == "note" else "<p>x</p>", note_id=nid)
        c.set_attributes(note_id=nid, values={"swarmCard": "", "taskId": tid, "section": sect,
                                              "cardKind": kind, "doneAt": "42", "updatedAt": "7"})
    c.create_note(parent_note_id="awmSwarmColTriage", title="context", type="text", content="keep me",
                  note_id="triageNote")


def test_the_migration_imports_then_empties_the_old_folders_into_history(world):
    fake, fence, alcor, w = world
    legacy_vault(fence)
    snaps = []
    out = migrate.migrate(fence, remote(alcor), snaps.append)
    assert snaps == ["pre-board-import"]
    sent = alcor.imports[0]
    assert [c["task_id"] for c in sent["cards"]] == ["d1", "p1", "w1"]
    assert {c["task_id"]: c.get("body") for c in sent["cards"]}["p1"] == "Tony wrote this"
    assert out["moved"] == 3
    for gone in ("awmSwarmColProposed", "awmSwarmColDone", "awmSwarmColTriage"):
        assert gone not in fake.notes
    assert fake.notes["triageNote"]["parents"] == [ids.COLUMN_IDS["history"]]
    assert fake.notes[ids.card_note_id("p1")]["content"] == "Tony wrote this"
    d = fake.labels(ids.card_note_id("d1"))
    assert d["section"] == "history" and d["historyAt"] == "42" and "doneAt" not in d
    assert fake.labels(ids.TASKS_ID)[migrate.LAYOUT_LABEL] == migrate.LAYOUT


def test_the_migration_runs_once(world):
    fake, fence, alcor, w = world
    legacy_vault(fence)
    migrate.migrate(fence, remote(alcor), lambda n: None)
    assert migrate.migrate(fence, remote(alcor), lambda n: None) == {"skipped": True}
    assert len(alcor.imports) == 1


def test_a_mismatch_asks_tony_and_leaves_the_vault_alone(world):
    fake, fence, alcor, w = world
    legacy_vault(fence)
    alcor.import_answer = {"ok": False, "missing": ["x9"], "extra": ["d1"]}
    with pytest.raises(migrate.ImportRefused):
        migrate.migrate(fence, remote(alcor), lambda n: None)
    assert "awmSwarmColDone" in fake.notes
    assert "x9" in alcor.humans[0] and "d1" in alcor.humans[0]
    assert migrate.LAYOUT_LABEL not in fake.labels(ids.TASKS_ID)
    alcor.import_answer = {"ok": True}
    migrate.migrate(fence, remote(alcor), lambda n: None)
    assert "awmSwarmColDone" not in fake.notes


def test_the_ops_notices_travel_in_the_import(world):
    fake, fence, alcor, w = world
    install.install(fence)
    fence.create(ids.OPS_ID, "scope-hygiene - mizar", "code", "## Scope hygiene\n\n2 removed",
                 mime=ids.MARKDOWN, labels=ids.MANAGED)
    migrate.migrate(fence, remote(alcor), lambda n: None)
    sent = alcor.imports[0]["ops_notes"]
    assert [(n["kind"], n["node"]) for n in sent] == [("scope-hygiene", "mizar")]
