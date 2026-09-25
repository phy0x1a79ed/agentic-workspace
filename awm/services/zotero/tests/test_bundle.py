"""Folding Zotero's item list into references that can become notes.

Zotero returns references, attachments and notes as siblings related only by
`parentItem`, across several libraries whose keys are unique only within
themselves. What these tests hold down is the two ways that flattens wrongly:
a file promised but not present, and one library's item overwriting another's.
"""

from __future__ import annotations

import pytest

from awm.zotero import bundle

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def raw(key: str, item_type: str, **data) -> dict:
    return {"version": 1, "data": {"key": key, "itemType": item_type, **data}}


# -- the fields --------------------------------------------------------------


def test_an_institutional_author_has_a_name_and_no_surname():
    """Dropping it would silently lose the author of every standards document
    and government report in the library."""
    assert bundle.creators({"creators": [
        {"lastName": "Gruber", "firstName": "Nicolas"},
        {"name": "World Health Organization"}]}) == [
        "Gruber, Nicolas", "World Health Organization"]


def test_the_year_comes_out_of_a_date_zotero_did_not_normalize():
    assert bundle.year({"parsedDate": "2019-03-01"}) == "2019"
    assert bundle.year({"date": "Submitted 3 March 2019"}) == "2019"
    assert bundle.year({"date": "n.d."}) == ""


def test_a_date_that_is_not_a_year_is_not_mistaken_for_one():
    """Four digits are not enough — `1234 Main Street` is not a publication
    year, and a bad `#year` label is worse than none."""
    assert bundle.year({"date": "1234 Main Street"}) == ""


# -- the fold ----------------------------------------------------------------


def test_an_attachment_folds_into_its_parent(tmp_path):
    out = bundle.normalize(
        [raw("AAA", "journalArticle", title="A paper"),
         raw("BBB", "attachment", parentItem="AAA", filename="p.pdf")],
        [], {"BBB": "p.pdf"})
    assert out["items"][0]["files"] == {"BBB": "p.pdf"}


def test_an_attachment_with_no_bytes_on_disk_is_dropped():
    """`filename` is set on an attachment Zotero has only ever seen the
    metadata for. A bundle promising a file it does not hold makes the apply
    fail on data rather than on a mistake."""
    out = bundle.normalize(
        [raw("AAA", "journalArticle", title="A paper"),
         raw("BBB", "attachment", parentItem="AAA", filename="p.pdf")],
        [], {})
    assert "files" not in out["items"][0]


def test_zoteros_own_notes_ride_along_with_their_parent():
    out = bundle.normalize(
        [raw("AAA", "journalArticle", title="A paper"),
         raw("CCC", "note", parentItem="AAA", note="<p>read this</p>")],
        [], {})
    # Under the note's own key, so a window read carrying one note of three can
    # say which one it replaces.
    assert out["items"][0]["notes"] == {"CCC": "<p>read this</p>"}


def test_attachments_and_notes_are_not_references_themselves():
    out = bundle.normalize(
        [raw("AAA", "journalArticle", title="A paper"),
         raw("BBB", "attachment", parentItem="AAA"),
         raw("CCC", "note", parentItem="AAA")], [], {})
    assert len(out["items"]) == 1


def test_a_child_whose_parent_is_in_another_library_is_skipped():
    """Not an error — a group library's attachment can name a parent this pass
    is not reading. Attaching it to nothing is the only safe answer."""
    out = bundle.normalize([raw("BBB", "attachment", parentItem="ZZZ")], [], {})
    assert out["items"] == []


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


def test_a_collection_reference_carries_its_library_too():
    out = bundle.normalize(
        [raw("AAA", "journalArticle", title="A", collections=["COL"])],
        [{"data": {"key": "COL", "name": "papers"}}], {}, library="groups/1")
    assert out["items"][0]["collections"] == ["groups/1/COL"]
    assert out["collections"][0]["ref"] == "groups/1/COL"


def test_a_nested_collection_names_its_parent_the_same_way():
    out = bundle.normalize(
        [], [{"data": {"key": "KID", "name": "sub", "parentCollection": "TOP"}}],
        {}, library="groups/1")
    assert out["collections"][0]["parent"] == "groups/1/TOP"


# -- the file on disk --------------------------------------------------------


def test_a_stored_file_is_filed_under_its_libraries_key(tmp_path):
    """Zotero files everything under one flat `storage/<key>/` and relies on
    keys not colliding, which holds inside a library and not between them."""
    b = bundle.Bundle(tmp_path)
    assert b.file_for("groups/1/AAA", "p.pdf") == \
        tmp_path / bundle.CHUNK / "files" / "groups/1/AAA" / "p.pdf"


def test_pruning_walks_to_whatever_depth_a_library_id_has(tmp_path):
    """A library id is itself two segments. Assuming one directory level per
    library made `users/0` look like an unwanted key and deleted every file
    under it -- right after they were fetched."""
    b = bundle.Bundle(tmp_path)
    keep = b.file_for("users/0/KEEP", "p.pdf")
    drop = b.file_for("users/0/DROP", "q.pdf")
    for path in (keep, drop):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"%PDF-")

    assert b.prune_files({"users/0/KEEP"}) == 1
    assert keep.is_file() and not drop.exists()


def test_pruning_removes_a_library_that_has_gone_entirely(tmp_path):
    b = bundle.Bundle(tmp_path)
    gone = b.file_for("groups/9/OLD", "p.pdf")
    gone.parent.mkdir(parents=True)
    gone.write_bytes(b"%PDF-")
    assert b.prune_files(set()) == 1
    assert not (b.files / "groups").exists()


# -- reading it back ---------------------------------------------------------


def test_an_absent_bundle_reads_as_empty_rather_than_raising(tmp_path):
    b = bundle.Bundle(tmp_path)
    assert b.exists is False
    assert b.versions == {}


def test_the_versions_survive_the_round_trip(tmp_path):
    b = bundle.Bundle(tmp_path)
    b.write(bundle.merge([bundle.normalize([], [], {})],
                         {"users/0": 7, "groups/1": 9}))
    assert b.versions == {"users/0": 7, "groups/1": 9}


def test_the_file_is_written_sorted_so_the_diff_is_the_librarys(tmp_path):
    """`git log -p` on the bundle should say what changed in the library, not
    what order the API answered in."""
    b = bundle.Bundle(tmp_path)
    b.write(bundle.merge([bundle.normalize([], [], {})], {"b": 1, "a": 2}))
    text = b.library_json.read_text()
    assert text.index('"collections"') < text.index('"items"')


def test_a_pinned_bundle_can_be_rewritten(tmp_path):
    """Once DVC has pinned a pull, `library.json` is a read-only hardlink into
    the shared cache. Writing into it fails, and the mirror then stops the
    first time somebody adds a paper — which is the only time anyone looks."""
    b = bundle.Bundle(tmp_path)
    b.write(bundle.merge([bundle.normalize([], [], {})], {"users/0": 1}))
    b.library_json.chmod(0o444)

    b.write(bundle.merge([bundle.normalize([], [], {})], {"users/0": 2}))
    assert b.versions == {"users/0": 2}


def test_rewriting_leaves_the_cached_object_alone(tmp_path):
    """The hardlink is the cache object. A write that landed in it would
    corrupt that object for every other scope and every commit pinning it."""
    b = bundle.Bundle(tmp_path)
    b.write(bundle.merge([bundle.normalize([], [], {})], {"users/0": 1}))
    cached = tmp_path / "cached.json"
    cached.hardlink_to(b.library_json)
    b.library_json.chmod(0o444)

    b.write(bundle.merge([bundle.normalize([], [], {})], {"users/0": 2}))
    assert '"users/0": 1' in cached.read_text()
    assert cached.stat().st_nlink == 1


def test_two_reads_of_one_library_give_the_same_digest(tmp_path):
    """Zotero does not answer in a stable order, and the digest is how an
    apply-only node decides it has nothing to do. An order-sensitive one turns
    every pull into a full re-walk of the mirror."""
    items = [raw("AAA", "journalArticle", title="A"),
             raw("BBB", "journalArticle", title="B"),
             raw("CCC", "journalArticle", title="C")]
    a, b = bundle.Bundle(tmp_path / "a"), bundle.Bundle(tmp_path / "b")
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


def test_a_note_added_to_an_existing_paper_costs_no_request():
    """The paper did not change, so the window carries the note alone. Against a
    keyed mapping that is a dictionary update; against a bare list it would have
    meant fetching the paper's children back over the network."""
    before = held([raw("AAA", "journalArticle", title="A paper")])
    window = whole([raw("N9", "note", parentItem="AAA", note="<p>new</p>")])

    out = bundle.fold(before, window, [], [], library="users/0")

    assert out["items"][0]["notes"] == {"N9": "<p>new</p>"}


def test_a_key_that_left_is_dropped_as_a_paper_or_as_a_note():
    before = held([
        raw("AAA", "journalArticle", title="A paper"),
        raw("N1", "note", parentItem="AAA", note="<p>one</p>"),
        raw("BBB", "journalArticle", title="Another")])

    out = bundle.fold(before, whole([]), ["BBB", "N1"], [], library="users/0")

    assert [i["ref"] for i in out["items"]] == ["users/0/AAA"]
    assert "notes" not in out["items"][0], "an emptied mapping must be dropped"


def test_a_fold_leaves_another_librarys_papers_alone():
    """`fold` is handed the whole bundle. A window for one library must not be
    able to touch another's, or one save retires a shared group."""
    before = held([raw("AAA", "journalArticle", title="Mine")])
    before["items"] += held([raw("GGG", "journalArticle", title="Theirs")],
                            library="groups/1")["items"]

    out = bundle.fold(before, whole([]), [], [], library="users/0")

    assert [i["ref"] for i in out["items"]] == ["users/0/AAA"]


def test_one_paper_from_both_sides_makes_one_record():
    """Sorting is stable, so a part carrying a record another part already had
    leaves both in the list rather than one replacing the other."""
    part = whole([raw("AAA", "journalArticle", title="A paper")])
    merged = bundle.merge([part, part], {"users/0": 1})
    assert len(merged["items"]) == 1
