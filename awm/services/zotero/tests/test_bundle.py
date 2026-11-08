"""Folding Zotero's item list into references that can become notes.

Zotero returns references, attachments and notes as siblings related only by
`parentItem`, across several libraries whose keys are unique only within
themselves. What these tests hold down is the two ways that flattens wrongly:
a file promised but not present, and one library's item overwriting another's.
"""

from __future__ import annotations

import pytest

from awm.zotero import bundle

from .fakes import FakeVault, MemoryStore

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def raw(key: str, item_type: str, **data) -> dict:
    return {"version": 1, "data": {"key": key, "itemType": item_type, **data}}


# -- the fields --------------------------------------------------------------


# -- the fold ----------------------------------------------------------------


def test_an_attachment_folds_into_its_parent(tmp_path):
    out = bundle.normalize(
        [raw("AAA", "journalArticle", title="A paper"),
         raw("BBB", "attachment", parentItem="AAA", filename="p.pdf")],
        [], {"BBB": "p.pdf"})
    assert out["items"][0]["files"] == {"BBB": "p.pdf"}


# -- libraries ---------------------------------------------------------------


def test_an_item_is_identified_by_library_and_key():
    """A key is unique within a library, not between them. Two libraries may
    both hold `ABCD1234`, and flattening onto the bare key would make one
    paper overwrite the other."""
    mine = bundle.normalize([raw("SAME", "journalArticle", title="Mine")], [],
                            {}, library="users/0")
    theirs = bundle.normalize([raw("SAME", "journalArticle", title="Theirs")],
                              [], {}, library="groups/1")
    assert mine["items"][0]["ref"] != theirs["items"][0]["ref"]

    merged = bundle.merge([mine, theirs], {"users/0": 1, "groups/1": 1})
    assert len({i["ref"] for i in merged["items"]}) == 2


# -- reading it back ---------------------------------------------------------


def test_the_document_is_written_sorted_so_a_diff_is_the_librarys():
    """A diff of two copies should say what changed in the library, not what
    order the API answered in."""
    store = MemoryStore()
    bundle.Bundle(store).write(bundle.merge([bundle.normalize([], [], {})],
                                            {"b": 1, "a": 2}))
    text = store.blob.decode("utf-8")
    assert text.index('"collections"') < text.index('"items"')


def test_the_bundle_rides_on_the_root_note_through_the_vault():
    v = FakeVault()
    root = v.create(parent="root", title="Library")
    bundle.Bundle(bundle.Attached(v, root)).write(
        bundle.merge([bundle.normalize([], [], {})], {"users/0": 3}))
    assert set(v.notes[root]["attachments"]) == {bundle.LIBRARY_JSON}
    assert bundle.Bundle(bundle.Attached(v, root)).versions == {"users/0": 3}


def test_two_reads_of_one_library_give_the_same_digest():
    """Zotero does not answer in a stable order, and the digest is how a pass
    decides it has nothing to do. An order-sensitive one turns every pull into
    a full re-walk of the mirror."""
    items = [raw("AAA", "journalArticle", title="A"),
             raw("BBB", "journalArticle", title="B"),
             raw("CCC", "journalArticle", title="C")]
    a, b = bundle.Bundle(MemoryStore()), bundle.Bundle(MemoryStore())
    a.write(bundle.merge([bundle.normalize(items, [], {})], {"users/0": 1}))
    b.write(bundle.merge([bundle.normalize(reversed(items), [], {})],
                         {"users/0": 1}))
    assert a.digest == b.digest


def test_one_library_state_makes_one_record_whatever_the_answer_order():
    """Observed live: two whole reads of a library at the identical version
    produced different records for 44 of 823 papers, and the apply rewrote every
    one of them. Nothing had changed in Zotero.

    The lists inside a record all sit inside the fingerprint that decides
    whether a note is rewritten, and none of them is read in order by anything
    downstream. So they are ours to settle, and settling them is what lets a
    window read and a whole read be compared at all.
    """
    def read(order):
        return bundle.normalize(
            [raw("AAA", "journalArticle", title="A paper",
                 tags=[{"tag": t} for t in order],
                 collections=list(reversed(order))),
             *[raw(k, "note", parentItem="AAA", note=f"<p>{k}</p>")
               for k in order]],
            [], {})

    assert read(["b", "a", "c"]) == read(["c", "b", "a"])


def test_a_paper_with_no_notes_serialises_as_it_always_did():
    """Otherwise every fingerprint in the vault moves at once, and all 823
    papers are rewritten to say nothing new."""
    out = bundle.normalize([raw("AAA", "journalArticle", title="A paper")],
                           [], {})
    assert "notes" not in out["items"][0]


# -- folding a window into what is already held ------------------------------


def whole(items, collections=(), library="users/0"):
    return bundle.normalize(items, collections, {}, library=library)


def held(items, collections=(), library="users/0"):
    """A previous bundle, in the shape `library.json` holds."""
    part = whole(items, collections, library)
    return {"items": part["items"], "collections": part["collections"]}


def test_a_window_replaces_the_paper_it_carries():
    before = held([raw("AAA", "journalArticle", title="Old")])
    window = whole([raw("AAA", "journalArticle", title="New")])

    out = bundle.fold(before, window, [], [], library="users/0")

    assert [i["title"] for i in out["items"]] == ["New"]


def test_a_window_never_takes_a_papers_notes_wholesale():
    """A paper's notes are separate items with their own versions, so a window
    carrying an edited paper carries none of its unchanged notes. Replacing the
    record wholesale drops them, and the fingerprint then says the shortened
    note is current."""
    before = held([
        raw("AAA", "journalArticle", title="Old"),
        raw("N1", "note", parentItem="AAA", note="<p>one</p>"),
        raw("N2", "note", parentItem="AAA", note="<p>two</p>")])
    window = whole([raw("AAA", "journalArticle", title="New")])

    out = bundle.fold(before, window, [], [], library="users/0")

    assert out["items"][0]["notes"] == {"N1": "<p>one</p>", "N2": "<p>two</p>"}


def test_a_key_that_left_is_dropped_as_a_paper_or_as_a_note():
    before = held([
        raw("AAA", "journalArticle", title="A paper"),
        raw("N1", "note", parentItem="AAA", note="<p>one</p>"),
        raw("BBB", "journalArticle", title="Another")])

    out = bundle.fold(before, whole([]), ["BBB", "N1"], [], library="users/0")

    assert [i["ref"] for i in out["items"]] == ["users/0/AAA"]
    assert "notes" not in out["items"][0], "an emptied mapping must be dropped"
