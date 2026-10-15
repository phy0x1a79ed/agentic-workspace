"""Archived cards live in the `Archived` folder of their column and keep that column's state."""

from datetime import datetime, timedelta, timezone

import pytest

from awm.board.states import STATES
from awm.board.vault import Vault

from test_migrate import WRITES, legacy, snapshot

BOARD_LABEL = "federationBoard"
ID = "a" * 32


def request(board, party, to="collins", **kw):
    kw.setdefault("title", "Run the thing")
    kw.setdefault("body", "please")
    return board.post(party, kind="request", recipient=to, **kw)


def age(trilium, card_id, days):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")
    trilium.set_attr(trilium.card_note(card_id), "cardStateAt", stamp)


@pytest.fixture
def finished(board, trilium, tony, collins):
    """A Done card the sweep has archived."""
    card = request(board, tony)
    board.claim(collins, card["id"])
    board.complete(collins, card["id"], "ok")
    age(trilium, card["id"], 15)
    board.sweep()
    return card


# -- the folders --------------------------------------------------------------------


def test_ensure_makes_an_archived_folder_in_every_column_once(trilium, board_note, vault):
    vault.ensure_columns()
    vault.ensure_columns()
    Vault(trilium, board_label=BOARD_LABEL).ensure_columns()          # a fresh process finds them
    for state in STATES:
        (folder,) = trilium.children(trilium.column(state))
        assert trilium.notes[folder]["title"] == "Archived"
        assert trilium.label(folder, "archived") == ""
        assert trilium.label(folder, "cardArchiveFolder") == state
    assert vault.archives() == {s: trilium.archive_folder(s) for s in STATES}
    assert trilium.columns() == list(STATES)                           # none leaked up to the board


def test_reading_makes_no_archived_folder(trilium, board_note, vault):
    assert vault.list() == [] and vault.read(ID) is None and vault.archives() == {}
    assert trilium.columns() == []


# -- archiving ----------------------------------------------------------------------


def test_archiving_files_the_card_in_its_columns_archived_folder_and_keeps_its_state(
        board, trilium, finished, tony):
    note = trilium.card_note(finished["id"])
    assert trilium.label(note, "archived") == ""
    assert trilium.notes[note]["parentNoteIds"] == [trilium.archive_folder("done")]
    assert trilium.state_of(finished["id"]) == "done"
    card = board.get(tony, finished["id"])
    assert card["state"] == "done" and card["status"] == "done"
    assert board.sweep() == []


def test_a_rejected_card_is_filed_under_rejected_archived(board, trilium, tony, collins):
    card = request(board, tony)
    board.reject(collins, card["id"], "no")
    age(trilium, card["id"], 15)
    board.sweep()
    assert trilium.notes[trilium.card_note(card["id"])]["parentNoteIds"] == [
        trilium.archive_folder("rejected")]
    assert board.get(tony, card["id"])["state"] == "rejected"


def test_an_archived_card_is_read_by_id_and_left_out_of_the_list(board, trilium, finished, tony):
    assert board.list(tony) == []
    fresh = Vault(trilium, board_label=BOARD_LABEL)
    rec = fresh.read(finished["id"])
    assert rec["note_id"] == trilium.card_note(finished["id"])
    assert rec["state"] == "done" and rec["archived_in"] is True
    assert fresh.list() == []


def test_an_archived_card_still_takes_replies_and_messages(board, trilium, finished, tony, collins):
    reply = board.post(tony, kind="message", recipient="collins", title="Re", body="thanks",
                       reply_to=finished["id"])
    assert reply["reply_to"] == finished["id"]
    posted = board.message(tony, finished["id"], "one more thing")
    assert posted["seq"] == 2                                         # the reply joined the thread first
    note = trilium.card_note(finished["id"])
    (chat,) = trilium.children(note)
    assert sorted(trilium.notes[n]["content"] for n in trilium.children(chat)) == [
        "one more thing", "thanks"]
    assert [m["body"] for m in board.messages(collins, finished["id"])] == [
        "thanks", "one more thing"]
    board.sweep()
    assert trilium.notes[note]["parentNoteIds"] == [trilium.archive_folder("done")]


def test_a_card_unarchived_by_hand_and_left_in_the_folder_still_reads_its_state(
        board, trilium, finished, tony):
    trilium("attr_delete", {"note_id": trilium.card_note(finished["id"]), "name": "archived"})
    listed = board.list(tony)
    assert [c["id"] for c in listed] == [finished["id"]] and listed[0]["state"] == "done"


def test_archiving_twice_does_not_move_the_card_again(trilium, vault):
    rec = vault.create("t", "", {"cardId": ID}, {}, state="done")
    vault.archive(rec, "done")
    rec = vault.read(ID)
    moves = len([1 for fn, _ in trilium.calls if fn == "note_move"])
    vault.archive(rec, "done")
    assert len([1 for fn, _ in trilium.calls if fn == "note_move"]) == moves


# -- the migration ------------------------------------------------------------------


@pytest.fixture
def old_archive(board, trilium, board_note, tony):
    """Archived cards in the old places: under the board with a status, and directly in a column."""
    legacy(trilium, board_note, "b" * 32, "Done", archived="")
    legacy(trilium, board_note, "c" * 32, "Failed", archived="")
    legacy(trilium, board_note, "d" * 32, "Posted", archived="")
    legacy(trilium, board_note, "e" * 32, "Posted")                    # live, not archived
    flat = request(board, tony)
    trilium.set_attr(trilium.card_note(flat["id"]), "archived", "")
    return {"flat": flat["id"]}


def test_the_archive_dry_run_reports_the_plan_and_changes_nothing(board, trilium, old_archive):
    trilium_calls = len(trilium.calls)
    before = snapshot(trilium)
    report = board.migrate_archive(dry_run=True)
    assert report["dry_run"] is True and report["cards_to_move"] == 4 and report["already_filed"] == 0
    assert report["by_state"] == {"proposed": 2, "done": 1, "rejected": 1}
    assert report["folders_to_create"] == []
    assert snapshot(trilium) == before
    assert not [fn for fn, _ in trilium.calls[trilium_calls:] if fn in WRITES]


def test_the_archive_migration_makes_the_folders_a_board_without_them_lacks(board, trilium, board_note):
    legacy(trilium, board_note, "b" * 32, "Done", archived="")
    assert board.migrate_archive(dry_run=True)["folders_to_create"] == list(STATES)
    assert trilium.columns() == []
    board.migrate_archive(dry_run=False)
    assert trilium.columns() == list(STATES)
    assert trilium.state_of("b" * 32) == "done" and trilium.is_filed_archived("b" * 32)


def test_the_archive_apply_files_each_card_by_its_column_and_a_second_apply_changes_nothing(
        board, trilium, old_archive, tony):
    report = board.migrate_archive(dry_run=False)
    assert report["moved"] == 4
    assert [trilium.state_of(c * 32) for c in "bcd"] == ["done", "rejected", "proposed"]
    assert trilium.state_of(old_archive["flat"]) == "proposed"
    assert all(trilium.is_filed_archived(c) for c in [*(c * 32 for c in "bcd"), old_archive["flat"]])
    assert trilium.label(trilium.card_note("b" * 32), "status") is None
    assert not trilium.is_filed_archived("e" * 32)                      # a live card stays put
    assert board.get(tony, "b" * 32)["state"] == "done"
    assert [c["id"] for c in board.list(tony)] == ["e" * 32]
    before = snapshot(trilium)
    again = board.migrate_archive(dry_run=False)
    assert again["cards_to_move"] == 0 and again["already_filed"] == 4
    assert snapshot(trilium) == before
