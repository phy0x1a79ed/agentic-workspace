"""A folded card lives under the note of the card it was folded into, and reads in the home's state."""

import pytest

from awm.board.vault import Vault

from test_archive import BOARD_LABEL, request
from test_migrate import WRITES, snapshot


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


def test_the_homes_dry_run_reports_the_plan_and_changes_nothing(board, trilium, folded):
    before, calls = snapshot(trilium), len(trilium.calls)
    report = board.migrate_homes(dry_run=True)
    assert report["dry_run"] is True and report["cards_to_move"] == 3
    assert report["already_homed"] == 0 and report["home_missing"] == 1
    assert report["without_home"] == 2                                  # the home and the lone card
    assert report["by_home"] == {folded["home"]: {"title": "Factorio benchmark",
                                                  "to_move": 3, "already": 0}}
    assert snapshot(trilium) == before
    assert not [fn for fn, _ in trilium.calls[calls:] if fn in WRITES]


def test_the_homes_apply_files_each_folded_card_under_its_home_and_a_second_apply_changes_nothing(
        board, trilium, folded, tony):
    report = board.migrate_homes(dry_run=False)
    assert report["moved"] == 3
    assert all(under_home(trilium, folded, n) for n in "abc")
    assert not trilium.is_filed_archived(folded["a"])
    assert trilium.is_filed_archived(folded["lone"]) and trilium.is_filed_archived(folded["stray"])
    assert trilium.is_filed_archived(folded["home"])                    # the home itself stays filed
    assert board.get(tony, folded["a"])["state"] == "proposed"
    assert board.list(tony) == []
    before, calls = snapshot(trilium), len(trilium.calls)
    again = board.migrate_homes(dry_run=False)
    assert again["moved"] == 0 and again["cards_to_move"] == 0 and again["already_homed"] == 3
    assert again["by_home"][folded["home"]]["to_move"] == 0
    assert snapshot(trilium) == before
    assert not [fn for fn, _ in trilium.calls[calls:] if fn in WRITES]


def test_the_homes_apply_leaves_a_loop_of_homes_where_it_is(board, trilium, folded):
    trilium.set_attr(trilium.card_note(folded["home"]), "cardFolded", folded["a"])
    report = board.migrate_homes(dry_run=False)
    assert report["cyclic_skipped"] == 4 and report["moved"] == 0     # home, a, b and c all lead into the loop
    assert trilium.is_filed_archived(folded["a"])


def test_the_homes_dry_run_counts_a_card_that_names_itself_as_a_loop(board, trilium, folded):
    trilium.set_attr(trilium.card_note(folded["lone"]), "cardFolded", folded["lone"])
    report = board.migrate_homes(dry_run=True)
    assert report["cyclic_skipped"] == 1 and report["home_missing"] == 1


def test_a_card_folded_into_a_folded_card_goes_flat_under_the_top_home(board, trilium, folded, tony):
    trilium.set_attr(trilium.card_note(folded["a"]), "cardFolded", folded["b"])
    trilium.set_attr(trilium.card_note(folded["b"]), "cardFolded", folded["c"])
    trilium.set_attr(trilium.card_note(folded["c"]), "cardFolded", folded["home"])
    plan = board.migrate_homes(dry_run=True)
    assert plan["by_home"][folded["home"]]["to_move"] == 3 and list(plan["by_home"]) == [folded["home"]]
    board.migrate_homes(dry_run=False)
    assert all(under_home(trilium, folded, n) for n in "abc")
    assert board.get(tony, folded["a"])["state"] == "proposed"
    before = snapshot(trilium)
    assert board.migrate_homes(dry_run=False)["moved"] == 0 and snapshot(trilium) == before


def test_a_card_nested_under_an_intermediate_home_reads_and_is_lifted_to_the_top(
        board, trilium, folded, tony):
    trilium.set_attr(trilium.card_note(folded["a"]), "cardFolded", folded["b"])
    trilium.set_attr(trilium.card_note(folded["b"]), "cardFolded", folded["home"])
    trilium("note_move", {"note_id": trilium.card_note(folded["b"]),
                          "parent": trilium.card_note(folded["home"])})
    trilium("note_move", {"note_id": trilium.card_note(folded["a"]),
                          "parent": trilium.card_note(folded["b"])})
    fresh = Vault(trilium, board_label=BOARD_LABEL)
    assert fresh.read(folded["a"])["state"] == "proposed"               # reads through the chain
    report = board.migrate_homes(dry_run=False)
    assert report["moved"] == 2 and under_home(trilium, folded, "a")       # a lifted, c filed
