"""The checkout contract: two writers, one note, nothing lost silently.

Each test here is named after a failure it prevents. The motivating one is
``test_a_direct_file_write_is_not_lost_silently``: an agent edited a note's
``.md`` while a browser had it open, the edit looked like it worked, and the
next flush erased it.

"A browser types into the note" is spelled ``notes.collab_edit`` throughout —
that is exactly what the page does per keystroke, and it is what makes the
live text diverge from the file.
"""

from __future__ import annotations

import sqlite3

import pytest

from awm.notes import checkout, config, db, index, notes, rooms, store
from awm.persistence.embeddings import EMBEDDINGS_DDL

# Separated edit sites, so a line merge has room to be unambiguous. Two writers
# touching the *same* line is the conflict case, and has its own tests.
DOC = """# Fabfos results

## Figures

![fig one](old/one.png)
<!-- figures-end -->

## Notes

Body text that nobody touches.

## Appendix

Trailing material.
<!-- appendix-end -->
"""

#: Insertion points far enough apart that a line merge is unambiguous, and
#: stable across rounds so a repeated edit stacks instead of re-matching itself.
FIGURES_END = "<!-- figures-end -->"
APPENDIX_END = "<!-- appendix-end -->"


def _insert_before(text: str, marker: str, line: str) -> str:
    return text.replace(marker, f"{line}\n{marker}")


@pytest.fixture(autouse=True)
def _clean_rooms():
    rooms._ROOMS.clear()
    yield
    rooms._ROOMS.clear()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    files = tmp_path / "files"
    files.mkdir()
    monkeypatch.setattr(config, "files_dir", lambda: files)

    orphaned = tmp_path / "orphaned"
    orphaned.mkdir()
    monkeypatch.setattr(config, "orphaned_dir", lambda: orphaned)

    # One registry per test, rooted in the test's own tmp dir.
    monkeypatch.setattr(checkout, "_REGISTRY", checkout.Checkouts(tmp_path / "checkouts"))

    monkeypatch.setattr(index, "embed_note", lambda *a, **k: None)
    monkeypatch.setattr(index, "drop_embedding", lambda *a, **k: None)
    monkeypatch.setattr(index, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(index, "probe", lambda: {"available": True, "missing": []})

    c = sqlite3.connect(tmp_path / "notes.db")
    c.row_factory = sqlite3.Row
    c.executescript(db.NOTES_DDL + EMBEDDINGS_DDL)
    yield c
    c.close()


@pytest.fixture()
def note(conn):
    """A note on disk, with no room open on it."""
    n = notes.create(conn, path="scadc/results", content=DOC)
    rooms.drop(n["id"])            # create() does not open one; be explicit
    return n["id"]


def _type_in_browser(note_id: str, text: str) -> None:
    """What the page does per keystroke: merge into the room, leaving the file behind."""
    rooms.open_room(note_id)
    notes.collab_edit(note_id, 0, text)


# ---------------------------------------------------------------------------
# The working copy is private
# ---------------------------------------------------------------------------


def test_checkout_gives_a_private_working_copy(conn, note):
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "new/one.png"))

    assert "new/one.png" in notes.checkout_read(h["id"])["content"]
    assert "old/one.png" in notes.get(conn, note)["content"]     # live untouched
    assert notes.checkout_status(h["id"])["ahead"] is True


def test_merge_lands_the_working_copy_and_closes_it(conn, note):
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "new/one.png"))
    res = notes.checkout_merge(conn, h["id"])

    assert res["kept"] is False
    assert "new/one.png" in notes.get(conn, note)["content"]
    assert "new/one.png" in store.read(note)
    assert notes.list_checkouts(note)["count"] == 0


def test_merge_can_keep_the_checkout_open(conn, note):
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC + "\nfirst pass\n")
    notes.checkout_merge(conn, h["id"], keep=True)

    st = notes.checkout_status(h["id"])
    assert st["ahead"] is False and st["behind"] is False   # rebased onto what it landed
    notes.checkout_write(h["id"], DOC + "\nfirst pass\nsecond pass\n")
    notes.checkout_merge(conn, h["id"])
    assert "second pass" in notes.get(conn, note)["content"]


# ---------------------------------------------------------------------------
# Two writers
# ---------------------------------------------------------------------------


def test_disjoint_concurrent_edits_both_survive(conn, note):
    """The motivating case: an agent rewriting a figure path while a person
    types in the appendix. Neither should have to know about the other."""
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "new/one.png"))
    _type_in_browser(note, DOC.replace("Trailing material.", "Trailing material, revised."))

    upd = notes.checkout_update(h["id"])
    assert upd["conflicts"] == 0
    notes.checkout_merge(conn, h["id"])

    landed = notes.get(conn, note)["content"]
    assert "new/one.png" in landed
    assert "Trailing material, revised." in landed


def test_merge_refuses_while_behind(conn, note):
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "new/one.png"))
    _type_in_browser(note, DOC.replace("Trailing material.", "Trailing material, revised."))

    assert notes.checkout_status(h["id"])["behind"] is True
    with pytest.raises(checkout.Behind):
        notes.checkout_merge(conn, h["id"])


def test_stale_writer_cannot_revert_the_live_note(conn, note):
    """The prototype failure this contract exists to make unrepresentable: a
    long-held copy landing on top of everything that happened since."""
    h = notes.take_checkout(conn, note)                      # pinned at the old text
    _type_in_browser(note, DOC + "\nWork done while the checkout was open.\n")

    with pytest.raises(checkout.Behind):
        notes.checkout_merge(conn, h["id"])                  # unchanged working copy
    assert "Work done while the checkout was open." in notes.get(conn, note)["content"]


def test_true_conflict_is_reported_not_resolved(conn, note):
    """Both writers on the same line. A merge that cannot tell you it failed is
    not a merge, so this must surface as markers rather than a winner."""
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "agent/one.png"))
    _type_in_browser(note, DOC.replace("old/one.png", "person/one.png"))

    upd = notes.checkout_update(h["id"])
    assert upd["conflicts"] == 1
    body = notes.checkout_read(h["id"])["content"]
    assert "<<<<<<<" in body and "agent/one.png" in body and "person/one.png" in body
    with pytest.raises(checkout.Conflicted):
        notes.checkout_merge(conn, h["id"])


def test_manual_resolution_is_the_escape_hatch(conn, note):
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "agent/one.png"))
    _type_in_browser(note, DOC.replace("old/one.png", "person/one.png"))
    notes.checkout_update(h["id"])

    notes.checkout_write(h["id"], DOC.replace("old/one.png", "agreed/one.png"))
    notes.checkout_resolve(h["id"])
    notes.checkout_merge(conn, h["id"])
    assert "agreed/one.png" in notes.get(conn, note)["content"]


def test_resolve_refuses_while_markers_remain(conn, note):
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "agent/one.png"))
    _type_in_browser(note, DOC.replace("old/one.png", "person/one.png"))
    notes.checkout_update(h["id"])

    with pytest.raises(checkout.Conflicted):
        notes.checkout_resolve(h["id"])


def test_update_refuses_while_conflicted(conn, note):
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "agent/one.png"))
    _type_in_browser(note, DOC.replace("old/one.png", "person/one.png"))
    notes.checkout_update(h["id"])

    with pytest.raises(checkout.Conflicted):
        notes.checkout_update(h["id"])


def test_update_with_nothing_to_pull_is_a_noop(conn, note):
    h = notes.take_checkout(conn, note)
    upd = notes.checkout_update(h["id"])
    assert upd["updated"] is False and upd["conflicts"] == 0


def test_long_lived_checkout_survives_many_live_revisions(conn, note):
    """A checkout held across a working session, updated each round."""
    h = notes.take_checkout(conn, note)
    for i in range(5):
        mine = notes.checkout_read(h["id"])["content"]
        notes.checkout_write(h["id"], _insert_before(mine, FIGURES_END, f"agent round {i}"))
        _type_in_browser(
            note, _insert_before(rooms.live_text(note), APPENDIX_END, f"person round {i}"),
        )
        assert notes.checkout_update(h["id"])["conflicts"] == 0

    notes.checkout_merge(conn, h["id"])
    landed = notes.get(conn, note)["content"]
    for i in range(5):
        assert f"agent round {i}" in landed
        assert f"person round {i}" in landed


# ---------------------------------------------------------------------------
# Landing against a live room
# ---------------------------------------------------------------------------


def test_merge_into_a_live_room_leaves_everything_agreeing(conn, note):
    """Room, file, row and keyword index are four copies of one text. A merge
    that updates some of them is how a note starts lying about itself."""
    _type_in_browser(note, DOC)                              # room open, same text
    h = notes.take_checkout(conn, note)
    landed_text = DOC.replace("old/one.png", "new/one.png")
    notes.checkout_write(h["id"], landed_text)
    res = notes.checkout_merge(conn, h["id"])

    assert rooms.live_content(note) == landed_text
    assert store.read(note) == landed_text
    row = db.get_note(conn, note)
    assert row["content_hash"] == db.content_hash(landed_text)
    fts = conn.execute("SELECT content FROM notes_fts WHERE note_id=?", (note,)).fetchone()
    assert fts["content"] == landed_text
    # A version to fan out, so open tabs adopt it without anyone typing.
    assert res["version"] is not None


def test_merge_does_not_leave_the_room_dirty(conn, note):
    """A room left dirty gets flushed again later, re-writing the merge over
    whatever happened next."""
    _type_in_browser(note, DOC)
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC + "\nlanded\n")
    notes.checkout_merge(conn, h["id"])
    assert rooms._ROOMS[note].dirty is False


# ---------------------------------------------------------------------------
# save, now that it goes through the same machinery
# ---------------------------------------------------------------------------


def test_save_merges_with_the_room_instead_of_replacing_it(conn, note):
    """The recovery path the reporting agent had to take by hand: read the
    file, edit it, write it back — while a browser holds newer text."""
    _type_in_browser(note, DOC.replace("Trailing material.", "Trailing material, revised."))
    from_file = store.read(note).replace("old/one.png", "new/one.png")

    res = notes.save(conn, note, content=from_file)
    assert res["merged"] is True

    landed = notes.get(conn, note)["content"]
    assert "new/one.png" in landed                     # the caller's edit
    assert "Trailing material, revised." in landed     # and the typing it never saw


def test_save_refuses_a_true_conflict_and_names_the_checkout(conn, note):
    _type_in_browser(note, DOC.replace("old/one.png", "person/one.png"))
    from_file = store.read(note).replace("old/one.png", "agent/one.png")

    with pytest.raises(checkout.Conflicted) as exc:
        notes.save(conn, note, content=from_file)
    assert "checkout" in str(exc.value)


def test_save_with_a_base_rev_makes_the_merge_base_exact(conn, note):
    read = notes.get(conn, note)
    _type_in_browser(note, DOC.replace("Trailing material.", "Trailing material, revised."))

    notes.save(conn, note, content=read["content"].replace("old/one.png", "new/one.png"),
               base_rev=read["rev"])
    landed = notes.get(conn, note)["content"]
    assert "new/one.png" in landed and "Trailing material, revised." in landed


def test_save_refuses_a_base_it_can_no_longer_reach(conn, note):
    with pytest.raises(checkout.Behind):
        notes.save(conn, note, content="whatever", base_rev="0" * 16)


def test_save_without_a_room_still_just_writes(conn, note):
    notes.save(conn, note, content="replaced entirely\n")
    assert notes.get(conn, note)["content"] == "replaced entirely\n"


def test_title_only_save_keeps_the_live_body_in_the_index(conn, note):
    """Renaming an open note used to re-index it from the file, so its search
    text silently regressed to the pre-edit copy until the next flush."""
    _type_in_browser(note, DOC + "\nthe unmistakable word zarquon\n")
    notes.save(conn, note, path="scadc/results/renamed")

    fts = conn.execute("SELECT content FROM notes_fts WHERE note_id=?", (note,)).fetchone()
    assert "zarquon" in fts["content"]
    assert notes.get(conn, note)["path"] == "scadc/results/renamed"


# ---------------------------------------------------------------------------
# The trap that started this
# ---------------------------------------------------------------------------


def test_a_direct_file_write_is_not_lost_silently(conn, note):
    """It still loses — the room holds text a person typed, and declining the
    flush would destroy that instead. But the bytes survive and it is logged."""
    _type_in_browser(note, DOC + "\ntyped in the browser\n")
    store.write(note, DOC.replace("old/one.png", "written/behind/the/service.png"))

    rooms.flush_all(conn)

    kept = list(config.orphaned_dir().glob(f"{note}.*.md"))
    assert len(kept) == 1
    assert "written/behind/the/service.png" in kept[0].read_text()
    assert "typed in the browser" in store.read(note)


def test_a_row_names_its_revision_and_flags_a_stale_file(conn, note):
    assert notes.get(conn, note).get("stale_file") is None
    _type_in_browser(note, DOC + "\nunflushed\n")

    row = notes.get(conn, note)
    assert row["stale_file"] is True
    assert row["rev"] == db.text_rev(row["content"])
    assert row["rev"] != db.text_rev(store.read(note))


def test_rev_distinguishes_whitespace_that_content_hash_does_not(conn, note):
    """`content_hash` normalises before hashing, so it cannot be the token a
    writer checks against — a reformat would read as no change at all."""
    a, b = "one\ntwo\n", "one\n\n\ntwo\n"
    assert db.content_hash(a) == db.content_hash(b)
    assert db.text_rev(a) != db.text_rev(b)


# ---------------------------------------------------------------------------
# Registry bookkeeping
# ---------------------------------------------------------------------------


def test_checkouts_are_listed_per_note(conn):
    a = notes.create(conn, path="a", content="alpha\n")["id"]
    b = notes.create(conn, path="b", content="beta\n")["id"]
    notes.take_checkout(conn, a)
    notes.take_checkout(conn, a)
    notes.take_checkout(conn, b)

    assert notes.list_checkouts(a)["count"] == 2
    assert notes.list_checkouts(b)["count"] == 1
    assert notes.list_checkouts()["count"] == 3


def test_discard_removes_the_working_copy(conn, note):
    h = notes.take_checkout(conn, note)
    path = checkout.registry().file_path(h["id"])
    assert path.exists()
    notes.checkout_discard(h["id"])
    assert not path.exists()
    assert notes.list_checkouts(note)["count"] == 0


def test_registry_survives_a_restart(conn, note, tmp_path):
    """Nothing about a checkout lives in memory, so a service restart mid-edit
    must leave it exactly where it was."""
    h = notes.take_checkout(conn, note)
    notes.checkout_write(h["id"], DOC.replace("old/one.png", "new/one.png"))

    fresh = checkout.Checkouts(tmp_path / "checkouts")      # as if the process restarted
    assert [x.id for x in fresh.list(note)] == [h["id"]]
    fresh.merge(conn, h["id"])
    assert "new/one.png" in notes.get(conn, note)["content"]


def test_checkout_of_a_missing_note_is_refused(conn):
    with pytest.raises(ValueError):
        notes.take_checkout(conn, "no-such-note")


def test_a_bad_handle_is_refused_rather_than_walking_the_filesystem(conn):
    for bad in ("../escape", "/etc/passwd", ".hidden", ""):
        with pytest.raises(checkout.CheckoutError):
            notes.checkout_status(bad)


def test_purging_a_note_takes_its_checkouts(conn, note):
    notes.take_checkout(conn, note)
    notes.purge(conn, note)
    assert notes.list_checkouts(note)["count"] == 0
