"""The Swarm board: layout, moves, and the notes the service generates."""

from __future__ import annotations

import pytest

from awm.reporting.swarmboard import BoardError, SwarmBoard

from stubs import FakeTrilium, make_board

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def tri():
    return FakeTrilium()


async def test_ensure_makes_the_board_once_under_awm(tri):
    awm = tri.add("AWM", type="book")
    board = SwarmBoard(tri)
    first = await board.ensure()
    assert first["created"] is True
    note = tri.notes[first["board"]]
    assert note["parents"] == [awm] and note["labels"] == {"swarmBoard": "", "board:columnsFrom": "folders"}
    assert [tri.notes[i]["title"] for i in tri.kids(first["board"])] == [
        "Proposed", "Planned", "Active", "Blocked", "Pending review", "Ledger"]
    before = len(tri.notes)
    second = await board.ensure()
    assert second["created"] is False and len(tri.notes) == before


async def test_ensure_refuses_when_awm_is_not_found_once(tri):
    with pytest.raises(BoardError, match="AWM"):
        await SwarmBoard(tri).ensure()


async def test_layout_refuses_when_the_board_is_missing(tri):
    with pytest.raises(BoardError, match="board_ensure"):
        await SwarmBoard(tri).layout()


async def test_layout_refuses_when_a_column_is_missing(tri):
    ids = make_board(tri)
    tri.notes.pop(ids["Blocked"])
    with pytest.raises(BoardError, match="Blocked"):
        await SwarmBoard(tri).layout()


async def test_move_adds_the_new_column_and_keeps_a_clone_on_tonys_board(tri):
    ids = make_board(tri)
    tonys = tri.add("Todo", type="book")
    card = tri.add("do it", tonys)
    board = SwarmBoard(tri)
    assert await board.move(card, "Proposed") is True
    assert set(tri.parents(card)) == {tonys, ids["Proposed"]}
    assert await board.move(card, "Planned") is True
    assert set(tri.parents(card)) == {tonys, ids["Planned"]}
    assert await board.move(card, "Planned") is False


async def test_move_never_uses_note_move_and_places_the_card_in_one_call(tri):
    make_board(tri)
    card = tri.add("x", tri.add("Tony", type="book"))
    board = SwarmBoard(tri)
    await board.move(card, "Active")
    assert [fn for fn in tri.writes()] == ["note_place"]


async def test_move_refuses_a_note_that_is_part_of_the_board(tri):
    ids = make_board(tri)
    with pytest.raises(BoardError, match="part of the board"):
        await SwarmBoard(tri).move(ids["Planned"], "Active")


async def test_a_card_made_for_an_untracked_session_lands_in_its_column(tri):
    ids = make_board(tri)
    card = await SwarmBoard(tri).create_card("intent", "Planned")
    assert tri.parents(card) == [ids["Planned"]] and tri.notes[card]["content"] == ""


async def test_snapshot_maps_every_card_to_its_column(tri):
    ids = make_board(tri)
    a, b = tri.add("a", ids["Active"]), tri.add("b", ids["Blocked"])
    assert await SwarmBoard(tri).snapshot() == {a: "Active", b: "Blocked"}


async def test_a_record_is_a_managed_markdown_child_and_the_card_body_is_never_written(tri):
    ids = make_board(tri)
    card = tri.add("do it", ids["Active"], content="Tony's words")
    board = SwarmBoard(tri)
    rec = await board.write_record(card, "one")
    again = await board.write_record(card, "two")
    assert rec == again
    note = tri.notes[rec]
    assert (note["type"], note["mime"], note["title"]) == ("code", "text/x-markdown", "Record")
    assert note["labels"] == {"agentManaged": "", "readOnly": ""} and note["content"] == "two"
    assert tri.notes[card]["content"] == "Tony's words"
    assert all(args.get("note_id") != card for fn, args in tri.calls if fn in ("note_update", "note_place"))


async def test_a_ledger_note_per_node_is_managed(tri):
    ids = make_board(tri)
    board = SwarmBoard(tri)
    note = await board.write_ledger("altair", "x")
    assert await board.write_ledger("altair", "y") == note
    assert tri.parents(note) == [ids["Ledger"]] and tri.notes[note]["content"] == "y"
    assert set(tri.notes[note]["labels"]) == {"agentManaged", "readOnly"}


async def test_a_trilium_failure_is_a_board_error_and_forgets_the_layout(tri):
    make_board(tri)
    board = SwarmBoard(tri)
    await board.layout()
    tri.fail = RuntimeError("down")
    with pytest.raises(BoardError, match="down"):
        await board.snapshot()
    assert board._layout is None
