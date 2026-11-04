"""A folded card lives under the note of the card it was folded into, and reads in the home's state."""

import pytest

from awm.board.vault import Vault

from test_archive import BOARD_LABEL, request


def fold(board, card, home):
    """Archive the card as an earlier fold did: in its column's `Archived` folder, labelled `archived` and with its home."""
    rec = board.vault.read(card["id"])
    board.vault.archive(rec, rec["state"], {"cardFolded": home["id"], "archived": ""})


@pytest.fixture
def folded(board, trilium, tony, collins):
    """A home card, three cards folded into it, a card with no home, and one whose home is gone."""
    ids = {"home": request(board, tony, title="Factorio benchmark")}
    for name in "abc":
        ids[name] = request(board, tony, title=f"Re: {name}")
    ids["lone"] = request(board, tony, title="orphan")
    ids["stray"] = request(board, tony, title="Re: stray")
    for name in "abc":
        fold(board, ids[name], ids["home"])
    fold(board, ids["stray"], {"id": "f" * 32})
    for name in ("home", "lone"):
        rec = board.vault.read(ids[name]["id"])
        board.vault.archive(rec, rec["state"])
    return {name: card["id"] for name, card in ids.items()}


def under_home(trilium, folded, name):
    return trilium.is_under(folded[name], folded["home"])


# -- reading a card under its home ----------------------------------------------------


def test_a_card_under_its_home_reads_in_the_homes_state_and_stays_out_of_the_list(
        board, trilium, folded, tony, collins):
    board.claim(collins, folded["home"])
    rec = board.vault.read(folded["home"])
    board.vault.archive(rec, "planned")
    trilium("note_move", {"note_id": trilium.card_note(folded["a"]),
                          "parent": trilium.card_note(folded["home"])})
    fresh = Vault(trilium, board_label=BOARD_LABEL)
    got = fresh.read(folded["a"])
    assert got["state"] == "planned" and got["archived_in"] is True
    assert got["home_note"] == trilium.card_note(folded["home"])
    assert board.get(tony, folded["a"])["state"] == "planned"
    assert folded["a"] not in [c["id"] for c in board.list(tony)]
    assert folded["a"] in [r["labels"]["cardId"] for r in fresh.list_archived()]


def test_a_card_under_its_home_still_takes_replies_and_messages(board, trilium, folded, tony, collins):
    trilium("note_move", {"note_id": trilium.card_note(folded["a"]),
                          "parent": trilium.card_note(folded["home"])})
    reply = board.post(collins, kind="message", recipient="tony", title="Re", body="thanks",
                       reply_to=folded["a"])
    assert reply["reply_to"] == folded["a"]
    assert board.message(tony, folded["a"], "more")["seq"] == 2
    assert [m["body"] for m in board.messages(collins, folded["a"])] == ["thanks", "more"]
    board.sweep()
    assert under_home(trilium, folded, "a")


def test_a_card_under_a_card_that_is_not_its_home_is_not_a_card(board, trilium, folded, tony):
    trilium("note_move", {"note_id": trilium.card_note(folded["a"]),
                          "parent": trilium.card_note(folded["lone"])})
    assert board.vault.read(folded["a"]) is None


def test_two_cards_folded_into_each_other_read_without_looping(board, trilium, folded):
    for name, other in (("a", "b"), ("b", "a")):
        trilium.set_attr(trilium.card_note(folded[name]), "cardFolded", folded[other])
    trilium("note_move", {"note_id": trilium.card_note(folded["a"]),
                          "parent": trilium.card_note(folded["b"])})
    assert Vault(trilium, board_label=BOARD_LABEL).read(folded["a"])["state"] in (None, "proposed")


# -- the migration ---------------------------------------------------------------------


