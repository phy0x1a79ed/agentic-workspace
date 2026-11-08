"""The board writer and the fence, against a fake vault (queue.md R61-R66, fleet.md R106 item 2)."""

from __future__ import annotations

import json

import pytest

from awm.trilium.board import ids, install, render
from awm.trilium.board.fence import Fence, Refused
from awm.trilium.board.writer import Writer

from .board_stubs import card, make_vault

pytestmark = [pytest.mark.unit]


@pytest.fixture
def world(monkeypatch):
    fake, fence = make_vault(monkeypatch)
    install.ensure_tasks(fence)
    return fake, fence, Writer(fence)


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
    fake, fence, w = world
    w.upsert(card("a", section))
    assert folder_of(fake, "a") == [column]
    assert fake.labels(ids.card_note_id("a"))["section"] == render.section_of({"section": section})


def test_a_card_moves_when_its_section_changes_and_keeps_its_note_id(world):
    fake, fence, w = world
    w.upsert(card("a", "working"))
    w.upsert(card("a", "stale"))
    w.upsert(card("a", "history", done_at=3000))
    assert folder_of(fake, "a") == ["awmSwarmColHistory"]
    labels = fake.labels(ids.card_note_id("a"))
    assert labels["historyAt"] == "3000" and "doneAt" not in labels


def test_chips_come_from_status_and_node_labels(world):
    fake, fence, w = world
    w.upsert(card("a", "needs_you", status="blocked", peer="alcor"))
    labels = fake.labels(ids.card_note_id("a"))
    assert (labels["status"], labels["peer"]) == ("blocked", "alcor")


# -- no null anywhere ---------------------------------------------------------------------------------------

def test_no_card_body_or_label_says_null(world):
    fake, fence, w = world
    w.upsert(card("a", "working", status=None, peer=None, meta={"reason": None, "todos": None},
                  sessions=[{"session_id": "s1", "name": None, "role": None, "active": False}]))
    nid = ids.card_note_id("a")
    body = fake.notes[nid]["content"]
    assert "null" not in body.lower() and "none" not in body.lower()
    assert not [v for v in fake.labels(nid).values() if v in ("None", "null")]


def test_a_proposed_note_keeps_its_text_and_a_null_body_is_rebuilt(world):
    fake, fence, w = world
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
    fake, fence, w = world
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
    fake, fence, w = world
    w.upsert(card("a"))
    w.upsert(card("a", folded_into="b"))
    assert ids.card_note_id("a") not in fake.notes


# -- the note a card was started from ---------------------------------------------------------------------------

@pytest.fixture
def source(world):
    fake, fence, w = world
    fence.c.create_note(parent_note_id="root", title="Paper", type="text", content="body",
                        note_id="srcNote")
    return world


def test_a_card_from_a_note_holds_a_clone_and_the_source_holds_a_marker(source):
    fake, fence, w = source
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
    fake, fence, w = source
    w.upsert(card("a", "working", source_note="srcNote"))
    w.upsert(card("a", "history", source_note="srcNote", done_at=5))
    assert ids.marker_note_id("a") not in fake.notes
    assert "srcNote" in fake.notes and fake.notes["srcNote"]["content"] == "body"
    assert fence.note("srcNote")["parentNoteIds"].count("root") == 1


def test_a_missing_source_note_links_nothing(world):
    fake, fence, w = world
    w.upsert(card("a", "working", source_note="gone"))
    assert ids.marker_note_id("a") not in fake.notes


def test_removing_a_clone_removes_only_the_branch(source):
    fake, fence, w = source
    w.upsert(card("a", "working", source_note="srcNote"))
    nid = ids.card_note_id("a")
    assert fence.unclone("srcNote", nid) is True
    assert "srcNote" in fake.notes and fake.notes["srcNote"]["parents"] == ["root"]
    assert fence.unclone("srcNote", nid) is False


def test_a_clone_is_never_removed_when_it_is_the_notes_only_branch(world):
    fake, fence, w = world
    w.upsert(card("a"))
    nid = ids.card_note_id("a")
    fence.c.create_note(parent_note_id=nid, title="only", type="text", content="x", note_id="solo")
    with pytest.raises(Refused):
        fence.unclone("solo", nid)
    assert "solo" in fake.notes


# -- the fence ----------------------------------------------------------------------------------------------------------

def test_a_write_outside_the_roots_is_refused(world):
    fake, fence, w = world
    fence.c.create_note(parent_note_id="root", title="mine", type="text", content="", note_id="mine")
    for call in (lambda: fence.create("mine", "x"), lambda: fence.update("mine", content="y"),
                 lambda: fence.delete("mine"), lambda: fence.move("mine", ids.TASKS_ID),
                 lambda: fence.relation("mine", "r", ids.TASKS_ID),
                 lambda: fence.update("mine", labels={"a": "b"})):
        with pytest.raises(Refused):
            call()
    assert fake.notes["mine"]["content"] == ""


def test_reads_go_anywhere_and_a_root_may_be_made_anywhere(world):
    fake, fence, w = world
    assert fence.note("srcNote") is None and fence.note(ids.BOOK_ID)["title"] == "AWM"
    assert fence.content(ids.BOOK_ID) == ""
    fence.create(ids.BOOK_ID, "Swarm board", "render", note_id=ids.ROOT_ID)
    assert ids.ROOT_ID in fake.notes


def test_a_root_never_moves_or_goes(world):
    fake, fence, w = world
    with pytest.raises(Refused):
        fence.move(ids.TASKS_ID, ids.BOOK_ID)
    with pytest.raises(Refused):
        fence.delete(ids.TASKS_ID)


def test_the_roots_are_this_hosts_setting(world, monkeypatch):
    fake, fence, w = world
    monkeypatch.setenv("AWM_TRILIUM_WRITER_ROOTS", "root")
    with pytest.raises(Refused, match="no writer subtree"):
        Fence()


def test_only_a_labelled_marker_is_deleted(world):
    fake, fence, w = world
    fence.c.create_note(parent_note_id="root", title="imposter", type="text", content="",
                        note_id=ids.marker_note_id("zz"))
    with pytest.raises(Refused):
        fence.drop_marker("zz")
    assert ids.marker_note_id("zz") in fake.notes


# -- the push ----------------------------------------------------------------------------------------------------------------

def test_a_page_applies_from_its_base_and_moves_the_mark(world):
    fake, fence, w = world
    assert w.apply(None, [], None) == {"mark": 0, "applied": 0}
    assert w.apply(0, [card("a", "stale"), card("b", "needs_you")], 3) == {"mark": 3, "applied": 2}
    assert folder_of(fake, "a") == ["awmSwarmColStale"]
    assert w.mark() == 3


def test_a_page_that_does_not_follow_the_vaults_mark_changes_nothing(world):
    fake, fence, w = world
    w.apply(0, [card("a")], 5)
    assert w.apply(2, [card("b")], 9) == {"mark": 5, "applied": 0}
    assert ids.card_note_id("b") not in fake.notes
    assert w.mark() == 5


def test_a_replay_changes_nothing_in_the_vault(world):
    fake, fence, w = world
    c = card("a", "working", sessions=[{"session_id": "s", "name": "n", "active": True}])
    w.apply(0, [c], 1)
    fake.calls.clear()
    w.upsert(c)
    writes = [c for c in fake.calls if c[0] in ("PATCH", "PUT", "POST", "DELETE")]
    assert writes == []


def test_one_bad_card_does_not_stop_the_rest(world):
    fake, fence, w = world
    bad_then_good = [{"section": "working"}, {"task_id": "b", "section": "working"}, card("a")]
    assert w.apply(0, bad_then_good, 3) == {"mark": 3, "applied": 2}
    assert ids.card_note_id("b") in fake.notes
