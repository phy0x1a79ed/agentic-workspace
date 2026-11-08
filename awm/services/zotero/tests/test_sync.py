"""What a mirror must not do to a knowledge base people also write in.

The apply runs on a timer over hundreds of notes, so the interesting
assertions are all negative: it must not touch a note somebody wrote, must not
duplicate a paper that is in three collections, must not rewrite anything on a
pass where nothing changed, and must not delete a note it did not create.
"""

from __future__ import annotations

import copy
import json

import pytest

from awm.zotero import bundle, sync

from .fakes import FakeVault, collection, item

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def v():
    return FakeVault()


def notes_touched(v, out, *verbs) -> set[str]:
    """Which notes these verbs hit, not counting the mirror's own status note.

    The status note is written once per pass that changed anything, which is
    correct and is exactly what makes a stalled mirror visible — but it is not a
    paper, and every assertion below is about papers."""
    status = (out.get("status_note") or {}).get("note_id")
    return v.touched(*verbs) - ({status} if status else set())


def seed(v, items: list[dict], collections: list[dict] | None = None,
         versions: dict[str, int] | None = None) -> bundle.Bundle:
    """Put a bundle on the library's root note, making the root as a first
    pass would."""
    root, _, _ = sync._resolve_root(v, parent="root", may_create=True)
    b = bundle.Bundle(bundle.Attached(v, root))
    b.write({"versions": versions or {"users/0": 1}, "pulled": "now",
             "collections": collections or [], "items": items})
    return b


# -- the shape it writes -----------------------------------------------------


def test_a_reference_becomes_a_note_carrying_its_citation_fields(v):
    seed(v, [item("AAA", title="Nitrogen", year="2008",
                      doi="10.1/x", creators=["Gruber, Nicolas"])])
    out = sync.apply(v)

    note = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]
    assert note["title"] == "Gruber 2008 — Nitrogen"
    assert note["labels"]["year"] == "2008"
    assert note["labels"]["doi"] == "10.1/x"
    assert note["labels"][sync.KEY_LABEL] == "users/0/AAA"


def test_the_abstract_is_escaped_because_it_is_somebody_elses_text(v):
    """A note in this vault runs on awm's own origin, and an abstract arrives
    from a publisher's metadata."""
    seed(v, [item("AAA", abstract="<script>alert(1)</script>")])
    out = sync.apply(v)
    body = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]["content"]
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_each_library_gets_its_own_shelf(v):
    """Two libraries may both have a collection called "papers", and merging
    them would put a shared group's reading list inside a personal one with
    nothing saying it had happened."""
    seed(v, [item("AAA", library="users/0", library_name="My Library"),
                 item("BBB", library="groups/1", library_name="BCB2")])
    out = sync.apply(v)
    shelves = [n["title"] for n in v.notes.values()
               if sync.LIBRARY_LABEL in n["labels"]]
    assert sorted(shelves) == ["BCB2", "My Library"]


def test_collections_become_a_tree_with_parents_before_children(v):
    """Zotero returns collections in no order, and a child made before its
    parent would land at the top of the library and stay there."""
    seed(v,
         [item("AAA", collections=["users/0/KID"])],
         [collection("KID", "sub", parent="users/0/TOP"),
          collection("TOP", "top")])
    out = sync.apply(v)

    folders = v.owned(out["library_note"], sync.COLLECTION_LABEL)
    kid, top = v.notes[folders["users/0/KID"]], folders["users/0/TOP"]
    assert kid["parents"] == [top]


def test_a_paper_in_several_collections_is_one_note_in_several_places(v):
    """Branches, not copies. Copies would let the same paper diverge from
    itself."""
    seed(v, [item("AAA", collections=["users/0/C1", "users/0/C2"])],
         [collection("C1", "one"), collection("C2", "two")])
    out = sync.apply(v)

    keyed = v.owned(out["library_note"], sync.KEY_LABEL)
    assert len(keyed) == 1
    assert len(v.notes[keyed["users/0/AAA"]]["parents"]) == 2


# -- what it does on the second pass -----------------------------------------


def test_a_second_apply_writes_nothing(v):
    """This runs on a timer. A pass that rewrites every note puts a revision on
    every note, every time."""
    seed(v, [item("AAA", title="Nitrogen", year="2008")],
         [collection("C1", "one")])
    sync.apply(v)
    v.calls.clear()
    sync.apply(v)
    assert "create" not in v.verbs
    assert not any(v.update(nid, title=n["title"], content=n["content"])
                   for nid, n in list(v.notes.items()))


def test_a_renamed_paper_keeps_its_note(v):
    """Identity is the Zotero key, not the title — so correcting a title in
    Zotero moves the note rather than orphaning it."""
    seed(v, [item("AAA", title="Nitogen")])
    out = sync.apply(v)
    first = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]

    seed(v, [item("AAA", title="Nitrogen")])
    sync.apply(v)
    assert v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"] == first
    assert v.notes[first]["title"] == "Nitrogen"


def test_an_item_that_left_zotero_takes_its_note(v):
    seed(v, [item("AAA"), item("BBB")])
    out = sync.apply(v)
    assert len(v.owned(out["library_note"], sync.KEY_LABEL)) == 2

    seed(v, [item("AAA")])
    again = sync.apply(v)
    assert again["removed"] == 1
    assert list(v.owned(out["library_note"], sync.KEY_LABEL)) == ["users/0/AAA"]


# -- what it must never touch ------------------------------------------------


def test_a_note_somebody_wrote_in_the_library_is_never_rewritten(v):
    """It carries no #zoteroKey, so every pass is blind to it. That is the
    whole of how a mirror and a person share one subtree."""
    seed(v, [item("AAA")])
    out = sync.apply(v)
    mine = v.create(parent=out["library_note"], title="my reading notes",
                    content="<p>thoughts</p>")

    seed(v, [])
    sync.apply(v)
    assert v.notes[mine]["title"] == "my reading notes"
    assert v.notes[mine]["content"] == "<p>thoughts</p>"


def test_the_mirror_only_removes_what_the_mirror_put_there(v):
    seed(v, [item("AAA")])
    out = sync.apply(v)
    mine = v.create(parent=out["library_note"], title="mine")

    seed(v, [])
    assert sync.apply(v)["removed"] == 1
    assert mine in v.notes


# -- the bundle has to be there ----------------------------------------------


# -- two syncs at once -------------------------------------------------------


def test_a_second_sync_is_refused_while_one_holds_the_lock(v):
    """Two applies each read "what is already in the vault" before the other
    has written it, so both decide the same paper is new and both create it.
    It happened here and left 216 doubled papers, with every call succeeding."""
    seed(v, [item("AAA")])
    with sync.exclusive():
        with pytest.raises(sync.Busy, match="another zotero sync"):
            sync.apply(v)


def test_a_doubled_note_is_collapsed_rather_than_left_for_ever(v):
    """Taking the first and ignoring the rest means every later pass makes the
    same choice and never looks at the other."""
    seed(v, [item("AAA", title="Nitrogen")])
    out = sync.apply(v)
    original = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]

    twin = v.create(parent=out["library_note"], title="Nitrogen",
                    labels={sync.KEY_LABEL: "users/0/AAA"})
    # Forced: a twin made by hand does not move the bundle, and the cursor
    # means the mirror no longer re-walks the vault to find one. `force` is
    # the repair, and this is the deliberate cost of the cursor.
    again = sync.apply(v, force=True)
    assert again["deduplicated"] == 1
    assert twin not in v.notes and original in v.notes


def test_the_oldest_copy_is_the_one_kept(v):
    """It is the one somebody may already have linked to."""
    seed(v, [item("AAA")])
    out = sync.apply(v)
    first = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]
    v.create(parent=out["library_note"], title="dupe",
             labels={sync.KEY_LABEL: "users/0/AAA"})
    sync.apply(v, force=True)
    assert v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"] == first


# -- the reference itself ----------------------------------------------------


def test_a_javascript_url_does_not_become_a_link(v):
    """Escaping makes publisher metadata safe as text and does nothing to an
    href, and these notes run on awm's own origin."""
    seed(v, [item("AAA", url="javascript:alert(1)")])
    out = sync.apply(v)
    body = v.notes[v.owned(out["library_note"],
                           sync.KEY_LABEL)["users/0/AAA"]]["content"]
    assert "<a href=" not in body
    assert "javascript:alert(1)" in body


# -- where the library goes --------------------------------------------------


def test_a_labelled_note_is_used_as_the_root(v):
    chosen = v.create(parent="root", title="Bibliography",
                      content="<p>my own words</p>",
                      labels={sync.ROOT_LABEL: ""})
    seed(v, [item("AAA")])
    out = sync.apply(v)
    assert out["library_note"] == chosen
    assert out["root_from"] == "label"


def test_the_chosen_notes_own_body_and_label_are_left_alone(v):
    """`ensure` replaces the body of a title match and `set_label` patches the
    value — so the label branch calls neither."""
    chosen = v.create(parent="root", title="Bibliography",
                      content="<p>my own words</p>",
                      labels={sync.ROOT_LABEL: ""})
    seed(v, [item("AAA")])
    sync.apply(v)
    assert v.notes[chosen]["content"] == "<p>my own words</p>"
    assert v.notes[chosen]["labels"][sync.ROOT_LABEL] == ""


def test_two_labelled_notes_refuse_and_name_both(v):
    """A root that alternates between passes builds the whole library under
    one, then builds it under the other and deletes the first."""
    seed(v, [item("AAA")])
    a = v.create(parent="root", title="One", labels={sync.ROOT_LABEL: "1"})
    b = v.create(parent="root", title="Two", labels={sync.ROOT_LABEL: "1"})
    with pytest.raises(sync.Ambiguous) as e:
        sync.apply(v)
    assert a in str(e.value) and b in str(e.value)


# -- archived notes ----------------------------------------------------------


def test_an_archived_paper_is_updated_rather_than_duplicated(v):
    """Archiving a mirrored note must not make the mirror create a second copy
    carrying the same key — and the collapse pass, using the same search,
    could not see the original to collapse it."""
    seed(v, [item("AAA", title="Nitrogen")])
    out = sync.apply(v)
    paper = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]

    v.archived.add(paper)
    seed(v, [item("AAA", title="Nitrogen, revised")])
    again = sync.apply(v)
    assert again["created"] == 0
    assert v.notes[paper]["title"].endswith("Nitrogen, revised")


# -- the cursor and the dry run ----------------------------------------------


def test_the_cursor_is_not_written_when_a_pass_dies_partway(v):
    """A pass that died halfway must retry rather than declare itself done."""
    seed(v, [item("AAA")])
    boom = RuntimeError("the vault went away")
    real = v.set_label

    def explode(note_id, name, value):
        if name == sync.STAMP_LABEL:
            raise boom
        return real(note_id, name, value)
    v.set_label = explode
    with pytest.raises(RuntimeError):
        sync.apply(v)
    roots = [n for n in v.notes.values() if sync.ROOT_LABEL in n["labels"]]
    assert sync.APPLIED_LABEL not in roots[0]["labels"]


def test_a_paper_whose_stamp_never_landed_is_written_again(v):
    """The reason the stamp is written last, and the case that decides it.

    A pass that died between the content and the file leaves a note that looks
    finished. Only the absent stamp says otherwise, so a stamp written any
    earlier would strand that paper for ever.
    """
    seed(v, [item("AAA"), item("BBB")])
    real = v.set_label

    def explode(note_id, name, value):
        if name == sync.STAMP_LABEL and v.notes[note_id]["labels"].get(
                sync.KEY_LABEL) == "users/0/BBB":
            raise RuntimeError("the vault went away")
        return real(note_id, name, value)
    v.set_label = explode
    with pytest.raises(RuntimeError):
        sync.apply(v)

    v.set_label = real
    v.calls.clear()
    out = sync.apply(v)
    assert out["unchanged"] == 1 and out["updated"] == 1
    assert len(notes_touched(v, out, "update")) == 1


def test_a_dry_run_reports_what_would_change_and_writes_nothing(v):
    seed(v, [item("AAA"), item("BBB")])
    before = copy.deepcopy(v.notes)
    out = sync.apply(v, dry_run=True)
    assert out["would_create"] == 2 and out["would_delete"] == 0
    assert out["would_visit"] == 2
    assert v.notes == before


# -- the per-paper cursor ----------------------------------------------------
#
# The mirror runs on a timer over hundreds of notes, and until these existed a
# single new paper rewrote every one of them. Each test below is a negative:
# what the pass must NOT touch.


def _bump(v, items, collections=None, versions=None):
    """Re-seed with a moved library version, so the root's digest guard lets the
    pass through and the per-paper cursor is what is actually under test."""
    return seed(v, items, collections,
                versions or {"users/0": 99})


def test_one_changed_paper_touches_exactly_one_note(v):
    seed(v, [item("AAA"), item("BBB"), item("CCC")])
    out = sync.apply(v)
    keyed = v.owned(out["library_note"], sync.KEY_LABEL)

    _bump(v, [item("AAA"), item("BBB", title="A better title", version=2),
                  item("CCC")])
    v.calls.clear()
    out = sync.apply(v)
    assert out["updated"] == 1 and out["unchanged"] == 2
    assert notes_touched(v, out, "update") == {keyed["users/0/BBB"]}


def test_version_zero_does_not_defeat_the_cursor(v):
    """A paper Zotero has not versioned must still be skippable. A predicate
    written on the version rather than on the fingerprint reads 0 as "unknown"
    and puts that paper on the slow path for ever."""
    papers = [item("AAA", version=0)]
    seed(v, papers)
    sync.apply(v)
    _bump(v, papers)
    v.calls.clear()
    assert sync.apply(v)["unchanged"] == 1
    assert v.touched("update") == set()


def test_a_renamed_library_rewrites_its_papers_though_no_version_moved(v):
    """One of the cases that decides the cursor.

    Renaming a shared group changes what every note in it must say and moves no
    item's version at all. A cursor keyed on Zotero's version would sail past
    it; one keyed on the whole record sees the name change.
    """
    seed(v, [item("AAA", library="groups/9", library_name="BCB2")])
    sync.apply(v)

    _bump(v, [item("AAA", library="groups/9", library_name="BCB Two")])
    v.calls.clear()
    out = sync.apply(v)
    assert out["updated"] == 1 and out["unchanged"] == 0
    paper = v.owned(out["library_note"], sync.KEY_LABEL)["groups/9/AAA"]
    assert v.notes[paper]["labels"]["zoteroLibraryName"] == "BCB Two"


# -- the collection axis, which needs no cursor of its own -------------------


def test_a_renamed_collection_re_places_no_paper(v):
    """Renaming a collection moves no paper: every note keeps the same parent
    note. That is why an item cursor sailing past a rename is not a problem."""
    papers = [item("AAA", collections=["users/0/C1"])]
    seed(v, papers, [collection("C1", "one")])
    sync.apply(v)
    keyed = set(v.owned("", sync.KEY_LABEL).values()) or {
        n for n, d in v.notes.items() if sync.KEY_LABEL in d["labels"]}

    _bump(v, papers, [collection("C1", "renamed")])
    v.calls.clear()
    out = sync.apply(v)
    assert out["unchanged"] == 1
    assert notes_touched(v, out, "update") and not (
        notes_touched(v, out, "update") & keyed)
    assert v.touched("place") == set()


def test_a_paper_dragged_out_of_place_by_hand_is_put_back(v):
    """Placement is checked on every pass whatever the cursor says, because the
    scan already reported where the note is. No cursor would ever notice this."""
    papers = [item("AAA", collections=["users/0/C1"])]
    seed(v, papers, [collection("C1", "one")])
    out = sync.apply(v)
    paper = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]
    v.notes[paper]["parents"] = [out["library_note"]]

    _bump(v, papers, [collection("C1", "one")])
    v.calls.clear()
    again = sync.apply(v)
    assert again["replaced"] == 1 and again["updated"] == 0
    assert paper in v.touched("place")


# -- what a pass costs -------------------------------------------------------


# -- saying so in the vault --------------------------------------------------


def test_a_persons_note_called_zotero_mirror_is_not_adopted(v):
    """`ensure` resolves a title under the root and would take this note over.
    The pointer is the identity; the title is decoration."""
    seed(v, [item("AAA")])
    out = sync.apply(v, dry_run=True)  # resolves nothing, writes nothing
    assert out["dry_run"] is True

    first = sync.apply(v)
    theirs = v.create(parent=first["library_note"], title=sync.STATUS_NOTE,
                      content="<p>my own reading notes</p>")

    _bump(v, [item("AAA", title="moved on", version=2)])
    again = sync.apply(v)
    assert again["status_note"]["note_id"] != theirs
    assert v.notes[theirs]["content"] == "<p>my own reading notes</p>"
