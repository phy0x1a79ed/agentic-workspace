"""A board a person and awm both write to.

The board view itself is upstream's, and these tests do not re-test it. What
they hold down is the arrangement this service produces for it — a column list
written where an empty column can exist, and a card identity that lets awm
rewrite its own cards without ever touching one somebody typed.
"""

from __future__ import annotations

import pytest

from awm.trilium import board, etapi

from .fake_vault import FakeVault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def _never_the_real_vault(monkeypatch):
    """No test in this file may reach a running Trilium.

    Not a precaution. An earlier version of the empty-column test called
    `board.ensure` without a fake, the call fell through to the default
    columns instead of being refused, and it created a real board in the live
    vault on this host. The `vault` fixture is opt-in per test and overrides
    this one; being unable to reach a socket is not something to remember.
    """
    def _refuse(*a, **kw):
        raise AssertionError(
            "a test reached the live vault — use the `vault` fixture")
    monkeypatch.setattr(etapi.httpx, "request", _refuse)


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    return fake


@pytest.fixture
def api():
    return etapi.client()


# -- the column list ---------------------------------------------------------


def test_the_definition_is_the_form_upstreams_parser_reads():
    """`promoted,alias=…,single,select,options=a;b;c`. Written by hand here
    because the board view will read it back with its own parser, and a token
    it does not recognize is skipped silently rather than reported."""
    assert board.definition_value(["To do", "Doing", "Done"]) == (
        "promoted,alias=Status,single,select,options=To do;Doing;Done")


def test_a_column_name_carrying_a_separator_survives_the_round_trip():
    """`,` ends a token and `;` ends an option, so both are escaped — and `%`
    first, or the escapes themselves would be escaped again."""
    value = board.definition_value(["a,b", "c;d", "100%"])
    assert value.endswith("options=a%2Cb;c%3Bd;100%25")
    assert board._decode_option("a%2Cb") == "a,b"
    assert board._decode_option("100%25") == "100%"


def test_the_columns_are_promoted_because_a_card_must_show_its_own():
    """An unpromoted field never renders on the card, which would leave the
    status reachable only by dragging."""
    assert board.definition_value(["A"]).startswith("promoted,")


def test_a_board_grouping_by_something_else_is_named_by_its_own_label():
    assert "alias=phase" in board.definition_value(["A"], group_by="phase")


# -- creating a board --------------------------------------------------------


def test_a_new_board_is_a_book_note_the_board_view_will_render(vault, api):
    out = board.ensure(api, title="Work")
    note = vault.notes[out["note_id"]]
    assert note["type"] == "book"
    labels = vault.labels(out["note_id"])
    assert labels["viewType"] == "board"
    assert labels["board:groupBy"] == "status"
    assert labels["label:status"].endswith("options=To do;Doing;Blocked;Done")


def test_the_default_columns_name_the_state_a_board_exists_to_show(vault, api):
    assert "Blocked" in board.ensure(api, title="Work")["columns"]


def test_a_second_ensure_changes_nothing(vault, api):
    board.ensure(api, title="Work")
    vault.calls.clear()
    again = board.ensure(api, title="Work")
    assert again["created"] is False and again["changed"] == []
    assert not [c for c in vault.calls if c[0] in ("POST", "PUT", "PATCH")]


def test_ensure_moves_an_existing_board_to_new_columns(vault, api):
    made = board.ensure(api, title="Work")
    again = board.ensure(api, title="Work", columns=["Now", "Later"])
    assert again["note_id"] == made["note_id"]
    assert vault.labels(made["note_id"])["label:status"].endswith(
        "options=Now;Later")


def test_an_empty_column_list_is_refused_rather_than_defaulted(vault, api):
    """Absent means "you choose"; empty means the caller worked out a column
    list and it came out empty. Answering the second with four invented
    columns would hide the bug that produced it."""
    with pytest.raises(ValueError, match="at least one column"):
        board.ensure(api, title="Work", columns=[])
    assert board.ensure(api, title="Work", columns=None)["columns"] == \
        list(board.DEFAULT_COLUMNS)


def test_two_notes_of_that_title_are_a_refusal_not_a_guess(vault, api):
    board.ensure(api, title="Work")
    api.create_note(parent_note_id="root", title="Work", type="book", content="")
    with pytest.raises(etapi.EtapiError, match="refusing to guess"):
        board.ensure(api, title="Work")


# -- cards -------------------------------------------------------------------


def test_a_card_is_created_with_its_key_and_its_column(vault, api):
    b = board.ensure(api, title="Work")["note_id"]
    card = board.card_upsert(api, board=b, key="t-1", title="Ship it",
                             status="Doing")
    assert card["created"] is True
    assert vault.labels(card["note_id"]) == {"awmKey": "t-1", "status": "Doing"}


def test_the_same_card_twice_writes_nothing(vault, api):
    b = board.ensure(api, title="Work")["note_id"]
    board.card_upsert(api, board=b, key="t-1", title="Ship it", status="Doing",
                      content="<p>x</p>")
    vault.calls.clear()
    again = board.card_upsert(api, board=b, key="t-1", title="Ship it",
                              status="Doing", content="<p>x</p>")
    assert again["created"] is False and again["changed"] == []
    assert not [c for c in vault.calls if c[0] in ("POST", "PUT", "PATCH")]


def test_a_card_is_matched_on_its_key_so_awm_may_rename_its_own(vault, api):
    b = board.ensure(api, title="Work")["note_id"]
    first = board.card_upsert(api, board=b, key="t-1", title="Ship it",
                              status="Doing")
    renamed = board.card_upsert(api, board=b, key="t-1", title="Ship it later",
                                status="Doing")
    assert renamed["note_id"] == first["note_id"]
    assert renamed["changed"] == ["title"]
    assert len(vault.notes[b]["children"]) == 1


def test_moving_a_card_is_a_label_change(vault, api):
    b = board.ensure(api, title="Work")["note_id"]
    card = board.card_upsert(api, board=b, key="t-1", title="Ship it",
                             status="Doing")
    moved = board.card_upsert(api, board=b, key="t-1", title="Ship it",
                              status="Done")
    assert moved["changed"] == ["status"]
    assert vault.labels(card["note_id"])["status"] == "Done"


def test_a_card_with_no_key_is_a_refusal(api):
    with pytest.raises(ValueError, match="key is required"):
        board.card_upsert(api, board="root", key="  ", title="x", status="A")


def test_the_same_key_on_two_boards_is_two_cards(vault, api):
    one = board.ensure(api, title="One")["note_id"]
    two = board.ensure(api, title="Two")["note_id"]
    a = board.card_upsert(api, board=one, key="t-1", title="x", status="Doing")
    b = board.card_upsert(api, board=two, key="t-1", title="x", status="Doing")
    assert a["note_id"] != b["note_id"]


def test_a_card_nested_under_another_card_is_still_on_the_board(vault, api):
    """The board view groups its subtree flattened and recursively, so
    matching only direct children would lose cards it is showing."""
    b = board.ensure(api, title="Work")["note_id"]
    top = board.card_upsert(api, board=b, key="t-1", title="x",
                            status="Doing")["note_id"]
    nested = api.create_note(parent_note_id=top, title="sub", type="text",
                             content="")["note"]["noteId"]
    api.set_attribute(note_id=nested, name=board.KEY_LABEL, value="t-2")
    api.set_attribute(note_id=nested, name="status", value="To do")

    again = board.card_upsert(api, board=b, key="t-2", title="sub renamed",
                              status="Done")
    assert again["note_id"] == nested and again["created"] is False


# -- the two sources ---------------------------------------------------------


def _hand_card(api, board_id: str, title: str, status: str) -> str:
    """A card as a person makes one: a note in the board with a status and no
    key."""
    nid = api.create_note(parent_note_id=board_id, title=title, type="text",
                          content="")["note"]["noteId"]
    api.set_attribute(note_id=nid, name="status", value=status)
    return nid


def test_an_awm_pass_never_touches_a_card_somebody_typed(vault, api):
    b = board.ensure(api, title="Work")["note_id"]
    hand = _hand_card(api, b, "mine", "To do")
    board.card_upsert(api, board=b, key="t-1", title="mine", status="Done")
    assert vault.notes[hand]["title"] == "mine"
    assert vault.labels(hand)["status"] == "To do"


def test_a_hand_card_titled_like_an_awm_card_is_still_a_different_card(vault, api):
    """Identity is the key, not the title — which is the whole reason the two
    sources can share one board."""
    b = board.ensure(api, title="Work")["note_id"]
    hand = _hand_card(api, b, "Ship it", "To do")
    mine = board.card_upsert(api, board=b, key="t-1", title="Ship it",
                             status="Doing")
    assert mine["note_id"] != hand


def test_the_board_reports_who_placed_each_card(vault, api):
    b = board.ensure(api, title="Work")["note_id"]
    _hand_card(api, b, "mine", "To do")
    board.card_upsert(api, board=b, key="t-1", title="theirs", status="Doing")
    out = board.cards(api, b)
    assert out["count"] == 2
    assert out["cards"]["To do"][0]["owner"] == "hand"
    assert out["cards"]["Doing"][0]["owner"] == "awm"
    assert out["columns"] == ["To do", "Doing", "Blocked", "Done"]


def test_an_empty_column_still_exists_because_the_definition_says_so(vault, api):
    """The one thing the attachment and the notes cannot express, and the
    reason the column list is written into the definition at all."""
    b = board.ensure(api, title="Work")["note_id"]
    out = board.cards(api, b)
    assert "Blocked" in out["columns"] and "Blocked" not in out["cards"]
