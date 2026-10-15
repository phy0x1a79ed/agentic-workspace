import pytest

from awm.board.vault import AmbiguousBoard, NoBoard, Vault, VaultError

ID = "a" * 32


def test_the_board_is_found_by_its_label(trilium, board_note, vault):
    trilium.add_note("Decoy titled Federation board")
    assert vault.board_id() == board_note


def test_no_board_refuses(trilium):
    with pytest.raises(NoBoard):
        Vault(trilium, board_label="federationBoard").board_id()


def test_two_boards_refuse(trilium, board_note, vault):
    trilium.add_note("Another", labels={"federationBoard": ""})
    with pytest.raises(AmbiguousBoard):
        vault.board_id()


def test_the_label_is_a_parameter(trilium, board_note):
    scratch = trilium.add_note("Scratch", labels={"scratchBoard": ""})
    assert Vault(trilium, board_label="scratchBoard").board_id() == scratch
    assert Vault(trilium).board_id() == board_note


def test_a_label_that_is_not_a_bare_name_is_refused(trilium):
    with pytest.raises(ValueError):
        Vault(trilium, board_label="x or #y")


def test_create_makes_a_direct_child_carrying_the_labels(trilium, board_note, vault):
    rec = vault.create("T", "the body", {"cardId": ID, "status": "Posted"}, {})
    assert trilium.notes[rec["note_id"]]["parentNoteIds"] == [board_note]
    assert rec["labels"] == {"cardId": ID, "status": "Posted"}
    assert rec["content"] == "the body"
    assert vault.read(ID)["note_id"] == rec["note_id"]


def test_create_records_a_relation(trilium, vault):
    first = vault.create("one", "", {"cardId": ID}, {})
    second = vault.create("two", "", {"cardId": "b" * 32}, {"replyTo": first["note_id"]})
    assert second["relations"] == {"replyTo": first["note_id"]}
    assert vault.card_id_for_note(first["note_id"]) == ID


def test_a_card_is_found_by_label_and_never_by_title(trilium, vault):
    vault.create("same title", "one", {"cardId": ID}, {})
    vault.create("same title", "two", {"cardId": "b" * 32}, {})
    assert vault.read(ID)["content"] == "one"
    assert vault.read("b" * 32)["content"] == "two"
    verbs = {fn for fn, _ in trilium.calls}
    assert "note_upsert" not in verbs


def test_read_finds_a_card_it_has_not_cached(trilium, board_note):
    writer = Vault(trilium, board_label="federationBoard")
    writer.create("t", "b", {"cardId": ID}, {})
    assert Vault(trilium, board_label="federationBoard").read(ID)["title"] == "t"


def test_unknown_or_malformed_ids_read_as_absent(vault):
    assert vault.read("c" * 32) is None
    assert vault.read("x or #cardId") is None


def test_notes_that_are_not_direct_children_are_not_cards(trilium, board_note, vault):
    other = trilium.add_note("elsewhere")
    trilium.add_note("stray", other, labels={"cardId": ID})
    nested = trilium.add_note("nested", board_note)
    trilium.add_note("deep", nested, labels={"cardId": "b" * 32})
    trilium.add_note("no id", board_note, labels={"status": "Posted"})
    assert vault.read(ID) is None
    assert vault.read("b" * 32) is None
    assert vault.list() == []


def test_two_notes_with_one_card_id_refuse(trilium, board_note, vault):
    trilium.add_note("a", board_note, labels={"cardId": ID})
    trilium.add_note("b", board_note, labels={"cardId": ID})
    with pytest.raises(VaultError):
        vault.read(ID)


def test_list_returns_every_card_without_bodies_unless_asked(vault):
    vault.create("one", "body one", {"cardId": ID}, {})
    vault.create("two", "body two", {"cardId": "b" * 32}, {})
    cards = vault.list()
    assert sorted(c["title"] for c in cards) == ["one", "two"]
    assert all(c["content"] is None for c in cards)
    assert sorted(c["content"] for c in vault.list(content=True)) == ["body one", "body two"]


def test_update_changes_labels_and_content(trilium, vault):
    rec = vault.create("t", "old", {"cardId": ID, "status": "Posted"}, {})
    vault.update(rec["note_id"], labels={"status": "Done"}, content="new")
    fresh = vault.read(ID)
    assert fresh["labels"]["status"] == "Done" and fresh["content"] == "new"


def test_a_transport_failure_is_a_vault_error(board_note):
    def broken(fn, args):
        raise ConnectionError("gateway down")

    with pytest.raises(VaultError):
        Vault(broken).board_id()


def test_the_board_is_resolved_once_until_refreshed(trilium, vault):
    vault.board_id()
    vault.board_id()
    searches = [a for fn, a in trilium.calls if fn == "note_search"]
    assert len(searches) == 1
    vault.board_id(refresh=True)
    assert len([1 for fn, _ in trilium.calls if fn == "note_search"]) == 2
