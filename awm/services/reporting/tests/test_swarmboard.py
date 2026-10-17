"""The Swarm board: layout, moves, and the notes the service generates."""

from __future__ import annotations

import re

import pytest

from awm.reporting import swarmboard
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
    assert note["parents"] == [awm]
    assert {k: v for k, v in note["labels"].items() if not k.startswith("label:")} == {
        "swarmBoard": "", "board:columnsFrom": "folders"}
    assert [tri.notes[i]["title"] for i in tri.kids(first["board"])] == [
        "Proposed", "Planned", "Active", "Blocked", "Pending review", "Done", "Rejected", "Ledger"]
    chips = {k: v for k, v in note["labels"].items() if k.startswith("label:")}
    assert chips == {"label:cardFrom": "promoted,single,text,alias=From",
                     "label:cardTo": "promoted,single,text,alias=To",
                     "label:project": "promoted,single,text,alias=Project",
                     "label:owner": "promoted,single,text,alias=Owner"}
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
    assert [fn for fn in tri.writes() if fn != "attr_set"] == ["note_place"]


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


async def test_a_claimed_cards_record_is_a_managed_html_child_and_its_body_is_never_written(tri):
    ids = make_board(tri)
    card = tri.add("do it", ids["Active"], content="Tony's words")
    old = tri.add("Record", card, type="code", content="md", mime="text/x-markdown",
                  labels={"agentManaged": "", "readOnly": ""})
    board = SwarmBoard(tri)
    rec = await board.write_record(card, "<p>one</p>")
    again = await board.write_record(card, "<p>two</p>")
    assert rec == again == old
    note = tri.notes[rec]
    assert (note["type"], note["mime"], note["title"]) == ("text", "text/html", "Record")
    assert note["labels"] == {"agentManaged": "", "readOnly": ""} and note["content"] == "<p>two</p>"
    assert tri.notes[card]["content"] == "Tony's words"
    assert all(args.get("note_id") != card for fn, args in tri.calls if fn in ("note_update", "note_place"))


async def test_a_record_made_fresh_is_a_managed_text_note(tri):
    ids = make_board(tri)
    card = tri.add("do it", ids["Active"])
    rec = await SwarmBoard(tri).write_record(card, "<p>x</p>")
    assert (tri.notes[rec]["type"], tri.notes[rec]["labels"]) == ("text", {"agentManaged": "", "readOnly": ""})


async def test_an_owned_card_takes_the_record_in_its_own_body_and_a_new_title(tri):
    make_board(tri)
    board = SwarmBoard(tri)
    card = await board.create_card("first", "Planned")
    assert tri.notes[card]["labels"] == {"agentManaged": "", "readOnly": ""}
    await board.write_card(card, "<p>body</p>", "better")
    assert (tri.notes[card]["title"], tri.notes[card]["content"]) == ("better", "<p>body</p>")
    await board.write_card(card, "<p>again</p>")
    assert tri.notes[card]["title"] == "better" and tri.kids(card) == []


async def test_drop_records_deletes_only_managed_record_children(tri):
    ids = make_board(tri)
    card = tri.add("c", ids["Active"])
    rec = tri.add("Record", card, type="code", labels={"agentManaged": ""})
    mine = tri.add("Record", card)
    board = SwarmBoard(tri)
    assert await board.drop_records(card, dry_run=True) == [rec] and rec in tri.notes
    assert await board.drop_records(card) == [rec]
    assert rec not in tri.notes and mine in tri.notes


async def test_an_ops_notice_shows_and_labels_when_it_changed_and_a_replacement_moves_it(tri, monkeypatch):
    tri.add("AWM", type="book")
    board = SwarmBoard(tri)
    await board.ensure()
    stamps = iter(["2026-10-12T09:00:00-07:00", "2026-10-12T10:30:00-07:00"])
    monkeypatch.setattr(swarmboard, "local_stamp", lambda utc=None: next(stamps))
    note = await board.post_ops("password", "altair", "Login", "one")
    assert tri.notes[note]["content"].startswith("Changed: 2026-10-12T09:00:00-07:00\n\n## Login")
    assert tri.notes[note]["labels"]["changed"] == "2026-10-12T09:00:00-07:00"
    assert await board.post_ops("password", "altair", "Login", "two") == note
    assert tri.notes[note]["content"].startswith("Changed: 2026-10-12T10:30:00-07:00")
    assert tri.notes[note]["labels"]["changed"] == "2026-10-12T10:30:00-07:00"
    assert "one" not in tri.notes[note]["content"]


def test_a_stamp_is_iso_8601_in_the_local_zone_with_its_offset():
    now = swarmboard.local_stamp()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", now)
    from datetime import datetime, timezone
    assert datetime.fromisoformat(swarmboard.local_stamp("2026-01-02 03:04:05.678Z")) == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert swarmboard.local_stamp("2026-01-02 03:04:05Z") == swarmboard.local_stamp("2026-01-02 03:04:05.000Z")


async def test_stamp_ops_backfills_only_the_notices_without_a_stamp(tri):
    tri.add("AWM", type="book")
    board = SwarmBoard(tri)
    ops = (await board.ensure())["ops"]
    old = tri.add("password - altair", ops, type="code", content="## Login\n\nbody",
                  labels={"agentManaged": "", "readOnly": ""})
    tri.notes[old]["modified"] = "2026-03-04 05:06:07.000Z"
    fresh = await board.post_ops("password", "capella", "Login", "b")
    seen = await board.stamp_ops(dry_run=True)
    assert [h["note_id"] for h in seen] == [old] and "changed" not in tri.notes[old]["labels"]
    await board.stamp_ops()
    assert tri.notes[old]["labels"]["changed"].startswith("2026-03-0")
    assert tri.notes[old]["content"].startswith("Changed: ") and "## Login\n\nbody" in tri.notes[old]["content"]
    assert await board.stamp_ops() == [] and tri.notes[fresh]["content"].count("Changed:") == 1


async def test_stamp_ops_leaves_tonys_own_notes_in_ops_alone(tri):
    tri.add("AWM", type="book")
    board = SwarmBoard(tri)
    ops = (await board.ensure())["ops"]
    mine = tri.add("Links", ops, type="code", content="# Me\n\nhttps://example.org")
    assert await board.stamp_ops() == []
    assert "changed" not in tri.notes[mine]["labels"] and tri.notes[mine]["content"].startswith("# Me")


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


async def test_a_board_made_before_done_and_rejected_gets_the_columns_on_first_use(tri):
    ids = make_board(tri)
    tri.notes.pop(ids["Done"])
    tri.notes.pop(ids["Rejected"])
    lay = await SwarmBoard(tri).layout()
    assert {tri.notes[lay.columns[t]]["title"] for t in ("Done", "Rejected")} == {"Done", "Rejected"}
    assert tri.notes[lay.columns["Done"]]["type"] == "book"
    assert tri.parents(lay.columns["Done"]) == [ids["board"]]


async def test_the_chips_are_defined_once_and_not_redefined_when_present(tri):
    make_board(tri)
    board = SwarmBoard(tri)
    await board.layout()
    await board.layout()
    sets = [a["name"] for fn, a in tri.calls if fn == "attr_set"]
    assert sorted(sets) == ["label:cardFrom", "label:cardTo", "label:owner", "label:project"]
    again = SwarmBoard(tri)
    await again.layout()
    assert len([1 for fn, _ in tri.calls if fn == "attr_set"]) == 4, "a second process finds them"


async def test_a_chip_definition_failure_costs_only_the_chips(tri, caplog):
    make_board(tri)
    board = SwarmBoard(tri)
    real = tri._attr_set

    def refuse(**args):
        raise RuntimeError("no")

    tri._attr_set = refuse
    with caplog.at_level("WARNING"):
        assert (await board.layout()).columns
    assert "could not define the card chips" in caplog.text and board._chips is False
    tri._attr_set = real


async def test_carry_labels_sets_only_what_is_missing_or_different(tri):
    make_board(tri)
    note = tri.add("card", labels={"cardFrom": "beta", "project": "old"})
    board = SwarmBoard(tri)
    assert await board.carry_labels(note, {"cardFrom": "beta", "cardTo": "tony", "project": "new",
                                           "owner": None}) is True
    assert tri.notes[note]["labels"] == {"cardFrom": "beta", "cardTo": "tony", "project": "new"}
    writes = len(tri.writes())
    assert await board.carry_labels(note, {"cardFrom": "beta", "cardTo": "tony"}) is False
    assert len(tri.writes()) == writes


async def test_fold_moves_the_card_into_older_and_keeps_a_clone_on_tonys_board(tri):
    ids = make_board(tri)
    tonys = tri.add("Todo", type="book")
    card = tri.add("x", tonys)
    board = SwarmBoard(tri)
    await board.move(card, "Done")
    assert await board.fold(card, "Done") is True
    assert set(tri.parents(card)) == {tonys, ids["Older Done"]}
    assert card not in await board.snapshot()


async def test_fold_waits_while_the_column_has_no_older_book(tri):
    ids = make_board(tri, older=False)
    card = tri.add("x", ids["Done"])
    assert await SwarmBoard(tri).fold(card, "Done") is False
    assert tri.parents(card) == [ids["Done"]]


async def test_a_board_without_older_books_is_read_again_until_it_has_them(tri):
    make_board(tri, older=False)
    board = SwarmBoard(tri)
    assert (await board.layout()).older == {}
    await board.ensure()
    assert set((await board.layout()).older) == {"Done", "Rejected"}
    reads = len(tri.calls)
    await board.layout()
    assert len(tri.calls) == reads


async def test_snapshot_does_not_take_an_older_book_for_a_card(tri):
    ids = make_board(tri)
    card = tri.add("x", ids["Done"])
    assert await SwarmBoard(tri).snapshot() == {card: "Done"}


async def test_move_refuses_an_older_book_and_takes_a_card_back_out_of_one(tri):
    ids = make_board(tri)
    board = SwarmBoard(tri)
    with pytest.raises(BoardError, match="part of the board"):
        await board.move(ids["Older Done"], "Active")
    card = tri.add("x", ids["Older Done"])
    assert await board.move(card, "Active") is True
    assert tri.parents(card) == [ids["Active"]]


async def test_a_move_dates_the_card_and_fill_only_dates_a_card_without_a_date(tri):
    ids = make_board(tri)
    board = SwarmBoard(tri)
    card = tri.add("x", ids["Active"])
    await board.move(card, "Done", stamp="move")
    first = tri.notes[card]["labels"]["cardStateAt"]
    assert first.endswith("+00:00")
    tri.notes[card]["labels"]["cardStateAt"] = "2026-01-01T00:00:00.000+00:00"
    await board.move(card, "Done", stamp="move")
    assert tri.notes[card]["labels"]["cardStateAt"] == "2026-01-01T00:00:00.000+00:00"
    await board.move(card, "Rejected", stamp="fill")
    assert tri.notes[card]["labels"]["cardStateAt"] == "2026-01-01T00:00:00.000+00:00"
    bare = tri.add("y", ids["Active"])
    await board.move(bare, "Done", stamp="fill")
    assert "cardStateAt" in tri.notes[bare]["labels"]


async def test_ensure_sorts_done_and_rejected_and_folds_the_archived_cards(tri):
    ids = make_board(tri, older=False)
    archived = tri.add("old", ids["Done"], labels={"archived": "", "cardStateAt": "2026-09-01T00:00:00.000+00:00"})
    undated = tri.add("undated", ids["Rejected"])
    tri.notes[undated]["modified"] = "2026-10-01 10:20:30.400Z"
    live = tri.add("live", ids["Done"], labels={"cardStateAt": "2026-10-15T00:00:00.000+00:00"})
    out = await SwarmBoard(tri).ensure()
    sort = {"sorted": "cardStateAt", "sortDirection": "desc"}
    for column in ("Done", "Rejected"):
        older = out["older"][column]
        assert tri.parents(older) == [ids[column]]
        assert tri.notes[older]["labels"] == {"swarmOlder": "", "cardStateAt": "0000", **sort}
        assert tri.notes[ids[column]]["labels"].items() >= sort.items()
    assert tri.parents(archived) == [out["older"]["Done"]] and "archived" not in tri.notes[archived]["labels"]
    assert tri.notes[undated]["labels"] == {"cardStateAt": "2026-10-01T10:20:30.400+00:00"}
    assert tri.parents(live) == [ids["Done"]]
    assert (out["folded"], out["stamped"]) == (1, 1)
    before = len(tri.notes)
    again = await SwarmBoard(tri).ensure()
    assert (again["folded"], again["stamped"]) == (0, 0) and len(tri.notes) == before


async def test_count_older_titles_each_book_by_its_cards_and_writes_only_a_change(tri):
    ids = make_board(tri)
    tri.add("a", ids["Older Done"])
    tri.add("b", ids["Older Done"])
    board = SwarmBoard(tri)
    await board.count_older()
    assert tri.notes[ids["Older Done"]]["title"] == "Older (2)"
    assert tri.notes[ids["Older Rejected"]]["title"] == "Older (0)"
    writes = len(tri.writes())
    await board.count_older()
    assert len(tri.writes()) == writes


async def test_done_and_rejected_are_columns_cards_can_be_placed_in(tri):
    ids = make_board(tri)
    card = tri.add("x", tri.add("Tony", type="book"))
    board = SwarmBoard(tri)
    await board.move(card, "Done")
    await board.move(card, "Rejected")
    assert ids["Rejected"] in tri.parents(card) and ids["Done"] not in tri.parents(card)
    assert (await board.snapshot())[card] == "Rejected"
