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
    assert out["items"][0]["notes"] == ["<p>read this</p>"]


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
