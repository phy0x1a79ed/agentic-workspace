"""What a mirror must not do to a knowledge base people also write in.

The apply runs on a timer over hundreds of notes, so the interesting
assertions are all negative: it must not touch a note somebody wrote, must not
duplicate a paper that is in three collections, must not rewrite anything on a
pass where nothing changed, and must not delete a note it did not create.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from awm.zotero import bundle, sync

from .fakes import FakeVault, collection, item

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def scope(tmp_path):
    return tmp_path


def seed(scope, items: list[dict], collections: list[dict] | None = None,
         versions: dict[str, int] | None = None) -> bundle.Bundle:
    b = bundle.Bundle(scope)
    b.root.mkdir(parents=True, exist_ok=True)
    b.library_json.write_text(json.dumps({
        "versions": versions or {"users/0": 1}, "pulled": "now",
        "collections": collections or [], "items": items}), "utf-8")
    return b


# -- the shape it writes -----------------------------------------------------


def test_a_reference_becomes_a_note_carrying_its_citation_fields(scope):
    seed(scope, [item("AAA", title="Nitrogen", year="2008",
                      doi="10.1/x", creators=["Gruber, Nicolas"])])
    v = FakeVault()
    out = sync.apply(v, scope)

    note = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]
    assert note["title"] == "Gruber 2008 — Nitrogen"
    assert note["labels"]["year"] == "2008"
    assert note["labels"]["doi"] == "10.1/x"
    assert note["labels"][sync.KEY_LABEL] == "users/0/AAA"


def test_a_field_zotero_does_not_have_is_left_off_rather_than_written_blank(scope):
    """`#doi` meaning "there is no DOI" would make `#doi` useless as a
    filter."""
    seed(scope, [item("AAA", title="No doi")])
    v = FakeVault()
    out = sync.apply(v, scope)
    note = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]
    assert "doi" not in note["labels"]


def test_the_abstract_is_escaped_because_it_is_somebody_elses_text(scope):
    """A note in this vault runs on awm's own origin, and an abstract arrives
    from a publisher's metadata."""
    seed(scope, [item("AAA", abstract="<script>alert(1)</script>")])
    v = FakeVault()
    out = sync.apply(v, scope)
    body = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]["content"]
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_each_library_gets_its_own_shelf(scope):
    """Two libraries may both have a collection called "papers", and merging
    them would put a shared group's reading list inside a personal one with
    nothing saying it had happened."""
    seed(scope, [item("AAA", library="users/0", library_name="My Library"),
                 item("BBB", library="groups/1", library_name="BCB2")])
    v = FakeVault()
    out = sync.apply(v, scope)
    assert sorted(v.titles_under(out["library_note"])) == ["BCB2", "My Library"]


def test_collections_become_a_tree_with_parents_before_children(scope):
    """Zotero returns collections in no order, and a child made before its
    parent would land at the top of the library and stay there."""
    seed(scope,
         [item("AAA", collections=["users/0/KID"])],
         [collection("KID", "sub", parent="users/0/TOP"),
          collection("TOP", "top")])
    v = FakeVault()
    out = sync.apply(v, scope)

    folders = v.owned(out["library_note"], sync.COLLECTION_LABEL)
    kid, top = v.notes[folders["users/0/KID"]], folders["users/0/TOP"]
    assert kid["parents"] == [top]


def test_a_paper_in_several_collections_is_one_note_in_several_places(scope):
    """Branches, not copies. Copies would let the same paper diverge from
    itself."""
    seed(scope, [item("AAA", collections=["users/0/C1", "users/0/C2"])],
         [collection("C1", "one"), collection("C2", "two")])
    v = FakeVault()
    out = sync.apply(v, scope)

    keyed = v.owned(out["library_note"], sync.KEY_LABEL)
    assert len(keyed) == 1
    assert len(v.notes[keyed["users/0/AAA"]]["parents"]) == 2


def test_a_paper_in_no_collection_sits_on_its_librarys_shelf(scope):
    seed(scope, [item("AAA", library_name="My Library")])
    v = FakeVault()
    out = sync.apply(v, scope)
    paper = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]
    shelf = [nid for nid, n in v.notes.items() if n["title"] == "My Library"]
    assert paper["parents"] == shelf


# -- what it does on the second pass -----------------------------------------


def test_a_second_apply_writes_nothing(scope):
    """This runs on a timer. A pass that rewrites every note puts a revision on
    every note, every time."""
    seed(scope, [item("AAA", title="Nitrogen", year="2008")],
         [collection("C1", "one")])
    v = FakeVault()
    sync.apply(v, scope)
    v.calls.clear()
    sync.apply(v, scope)
    assert "create" not in v.calls
    assert not any(v.update(nid, title=n["title"], content=n["content"])
                   for nid, n in list(v.notes.items()))


def test_a_renamed_paper_keeps_its_note(scope):
    """Identity is the Zotero key, not the title — so correcting a title in
    Zotero moves the note rather than orphaning it."""
    seed(scope, [item("AAA", title="Nitogen")])
    v = FakeVault()
    out = sync.apply(v, scope)
    first = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]

    seed(scope, [item("AAA", title="Nitrogen")])
    sync.apply(v, scope)
    assert v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"] == first
    assert v.notes[first]["title"] == "Nitrogen"


def test_an_item_that_left_zotero_takes_its_note(scope):
    seed(scope, [item("AAA"), item("BBB")])
    v = FakeVault()
    out = sync.apply(v, scope)
    assert len(v.owned(out["library_note"], sync.KEY_LABEL)) == 2

    seed(scope, [item("AAA")])
    again = sync.apply(v, scope)
    assert again["removed"] == 1
    assert list(v.owned(out["library_note"], sync.KEY_LABEL)) == ["users/0/AAA"]


# -- what it must never touch ------------------------------------------------


def test_a_note_somebody_wrote_in_the_library_is_never_rewritten(scope):
    """It carries no #zoteroKey, so every pass is blind to it. That is the
    whole of how a mirror and a person share one subtree."""
    seed(scope, [item("AAA")])
    v = FakeVault()
    out = sync.apply(v, scope)
    mine = v.create(parent=out["library_note"], title="my reading notes",
                    content="<p>thoughts</p>")

    seed(scope, [])
    sync.apply(v, scope)
    assert v.notes[mine]["title"] == "my reading notes"
    assert v.notes[mine]["content"] == "<p>thoughts</p>"


def test_the_mirror_only_removes_what_the_mirror_put_there(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    out = sync.apply(v, scope)
    mine = v.create(parent=out["library_note"], title="mine")

    seed(scope, [])
    assert sync.apply(v, scope)["removed"] == 1
    assert mine in v.notes


# -- files -------------------------------------------------------------------


def test_a_stored_file_is_attached_to_its_papers_note(scope):
    b = seed(scope, [item("AAA", files={"BBB": "p.pdf"})])
    path = b.file_for("users/0/BBB", "p.pdf")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.4")

    v = FakeVault()
    out = sync.apply(v, scope)
    note = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]
    assert note["attachments"] == {"p.pdf": 8}


def test_a_file_the_bundle_promises_but_does_not_hold_is_skipped(scope):
    """Not an error. A bundle carried to another node arrives with its pins
    unmaterialised, and failing the whole apply over one absent PDF would
    withhold the eight hundred references that are fine."""
    seed(scope, [item("AAA", files={"BBB": "p.pdf"})])
    v = FakeVault()
    out = sync.apply(v, scope)
    assert out["attached"] == 0
    assert v.owned(out["library_note"], sync.KEY_LABEL)


def test_an_unchanged_file_is_not_re_attached(scope):
    b = seed(scope, [item("AAA", files={"BBB": "p.pdf"})])
    path = b.file_for("users/0/BBB", "p.pdf")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.4")
    v = FakeVault()
    sync.apply(v, scope)
    # Forced, because an unchanged bundle is now skipped outright — see
    # `test_a_second_apply_of_an_unchanged_bundle_writes_nothing_at_all`.
    assert sync.apply(v, scope, force=True)["attached"] == 0


# -- the bundle has to be there ----------------------------------------------


def test_applying_without_a_bundle_says_where_to_get_one(scope):
    with pytest.raises(FileNotFoundError, match="zotero pull"):
        sync.apply(FakeVault(), scope)


# -- two syncs at once -------------------------------------------------------


def test_a_second_sync_is_refused_while_one_holds_the_lock(scope):
    """Two applies each read "what is already in the vault" before the other
    has written it, so both decide the same paper is new and both create it.
    It happened here and left 216 doubled papers, with every call succeeding."""
    seed(scope, [item("AAA")])
    with sync.exclusive(scope):
        with pytest.raises(sync.Busy, match="another zotero sync"):
            sync.apply(FakeVault(), scope)


def test_the_lock_is_released_when_its_holder_lets_go(scope):
    seed(scope, [item("AAA")])
    with sync.exclusive(scope):
        pass
    assert sync.apply(FakeVault(), scope)["items"] == 1


def test_a_doubled_note_is_collapsed_rather_than_left_for_ever(scope):
    """Taking the first and ignoring the rest means every later pass makes the
    same choice and never looks at the other."""
    seed(scope, [item("AAA", title="Nitrogen")])
    v = FakeVault()
    out = sync.apply(v, scope)
    original = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]

    twin = v.create(parent=out["library_note"], title="Nitrogen",
                    labels={sync.KEY_LABEL: "users/0/AAA"})
    # Forced: a twin made by hand does not move the bundle, and the cursor
    # means the mirror no longer re-walks the vault to find one. `force` is
    # the repair, and this is the deliberate cost of the cursor.
    again = sync.apply(v, scope, force=True)
    assert again["deduplicated"] == 1
    assert twin not in v.notes and original in v.notes


def test_the_oldest_copy_is_the_one_kept(scope):
    """It is the one somebody may already have linked to."""
    seed(scope, [item("AAA")])
    v = FakeVault()
    out = sync.apply(v, scope)
    first = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]
    v.create(parent=out["library_note"], title="dupe",
             labels={sync.KEY_LABEL: "users/0/AAA"})
    sync.apply(v, scope, force=True)
    assert v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"] == first


def test_sync_holds_the_lock_across_both_halves(scope, monkeypatch):
    """Pull-then-apply as two locked steps leaves a gap: a second sync starting
    between them applies the bundle the first has just rewritten, and both
    decide the same papers are new."""
    seed(scope, [item("AAA")])
    held: list[bool] = []

    def _pull(b, *, force, commit):
        try:
            with sync.exclusive(scope):
                held.append(False)
        except sync.Busy:
            held.append(True)
        return {"changed": True}

    monkeypatch.setattr(sync, "_pull", _pull)
    sync.run(FakeVault(), scope)
    assert held == [True], "the lock was not held while pulling"


# -- the reference itself ----------------------------------------------------


def test_the_journal_becomes_a_label_you_can_filter_on(scope):
    seed(scope, [item("AAA", publication="Nature")])
    v = FakeVault()
    out = sync.apply(v, scope)
    note = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]
    assert note["labels"]["publication"] == "Nature"


def test_an_item_with_no_journal_gets_no_publication_label(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    out = sync.apply(v, scope)
    note = v.notes[v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]]
    assert "publication" not in note["labels"]


def test_the_doi_reads_as_the_url_it_resolves_to(scope):
    """A bare identifier is not something a person can click, and the link
    text is the half that says where it goes."""
    seed(scope, [item("AAA", doi="10.1038/nature12373")])
    v = FakeVault()
    out = sync.apply(v, scope)
    body = v.notes[v.owned(out["library_note"],
                           sync.KEY_LABEL)["users/0/AAA"]]["content"]
    assert ('<a href="https://doi.org/10.1038/nature12373">'
            "https://doi.org/10.1038/nature12373</a>") in body


def test_a_javascript_url_does_not_become_a_link(scope):
    """Escaping makes publisher metadata safe as text and does nothing to an
    href, and these notes run on awm's own origin."""
    seed(scope, [item("AAA", url="javascript:alert(1)")])
    v = FakeVault()
    out = sync.apply(v, scope)
    body = v.notes[v.owned(out["library_note"],
                           sync.KEY_LABEL)["users/0/AAA"]]["content"]
    assert "<a href=" not in body
    assert "javascript:alert(1)" in body


def test_a_protocol_relative_url_is_still_a_link(scope):
    """The library holds exactly one, and an http/https-only guard would
    silently unlink it."""
    seed(scope, [item("AAA", url="//scripts.iucr.org/cgi-bin/paper")])
    v = FakeVault()
    out = sync.apply(v, scope)
    body = v.notes[v.owned(out["library_note"],
                           sync.KEY_LABEL)["users/0/AAA"]]["content"]
    assert '<a href="//scripts.iucr.org/cgi-bin/paper">' in body


# -- where the library goes --------------------------------------------------


def test_a_labelled_note_is_used_as_the_root(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    chosen = v.create(parent="root", title="Bibliography",
                      content="<p>my own words</p>",
                      labels={sync.ROOT_LABEL: ""})
    out = sync.apply(v, scope)
    assert out["library_note"] == chosen
    assert out["root_from"] == "label"


def test_the_chosen_notes_own_body_and_label_are_left_alone(scope):
    """`ensure` replaces the body of a title match and `set_label` patches the
    value — so the label branch calls neither."""
    seed(scope, [item("AAA")])
    v = FakeVault()
    chosen = v.create(parent="root", title="Bibliography",
                      content="<p>my own words</p>",
                      labels={sync.ROOT_LABEL: ""})
    sync.apply(v, scope)
    assert v.notes[chosen]["content"] == "<p>my own words</p>"
    assert v.notes[chosen]["labels"][sync.ROOT_LABEL] == ""


def test_no_labelled_note_and_no_permission_to_make_one_refuses(scope):
    seed(scope, [item("AAA")])
    with pytest.raises(sync.NoRoot, match="zoteroLibrary"):
        sync.apply(FakeVault(), scope, may_create=False)


def test_no_labelled_note_with_permission_creates_the_library(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    out = sync.apply(v, scope)
    assert out["root_from"] == "title"
    assert v.notes[out["library_note"]]["labels"][sync.ROOT_LABEL] == "1"


def test_two_labelled_notes_refuse_and_name_both(scope):
    """A root that alternates between passes builds the whole library under
    one, then builds it under the other and deletes the first."""
    seed(scope, [item("AAA")])
    v = FakeVault()
    a = v.create(parent="root", title="One", labels={sync.ROOT_LABEL: "1"})
    b = v.create(parent="root", title="Two", labels={sync.ROOT_LABEL: "1"})
    with pytest.raises(sync.Ambiguous) as e:
        sync.apply(v, scope)
    assert a in str(e.value) and b in str(e.value)


def test_a_keyed_note_outside_the_root_is_counted_but_not_touched(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    stray = v.create(parent="root", title="an orphan",
                     labels={sync.KEY_LABEL: "users/0/ZZZ"})
    out = sync.apply(v, scope)
    assert out["outside_root"] == 1
    assert stray in v.notes


# -- archived notes ----------------------------------------------------------


def test_an_archived_paper_is_updated_rather_than_duplicated(scope):
    """Archiving a mirrored note must not make the mirror create a second copy
    carrying the same key — and the collapse pass, using the same search,
    could not see the original to collapse it."""
    seed(scope, [item("AAA", title="Nitrogen")])
    v = FakeVault()
    out = sync.apply(v, scope)
    paper = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]

    v.archived.add(paper)
    seed(scope, [item("AAA", title="Nitrogen, revised")])
    again = sync.apply(v, scope)
    assert again["created"] == 0
    assert v.notes[paper]["title"].endswith("Nitrogen, revised")


def test_a_search_blind_to_archived_notes_is_what_doubles_a_paper(scope):
    """The failure the parameter exists to stop, shown rather than asserted
    about: with the flag off, the same pass creates a second copy."""
    seed(scope, [item("AAA", title="Nitrogen")])
    v = FakeVault()
    out = sync.apply(v, scope)
    paper = v.owned(out["library_note"], sync.KEY_LABEL)["users/0/AAA"]

    v.archived.add(paper)
    v.sees_archived = False
    seed(scope, [item("AAA", title="Nitrogen, revised")])
    assert sync.apply(v, scope)["created"] == 1


def test_archiving_the_root_does_not_hide_it_from_resolution(scope):
    """The flag is inherited, so one checkbox on a finished reference section
    would otherwise make the whole library invisible to the next pass."""
    seed(scope, [item("AAA")])
    v = FakeVault()
    chosen = v.create(parent="root", title="Bibliography",
                      labels={sync.ROOT_LABEL: "1"})
    sync.apply(v, scope)
    v.archived.add(chosen)
    again = sync.apply(v, scope, force=True)
    assert again["library_note"] == chosen
    assert again["created"] == 0


# -- the cursor and the dry run ----------------------------------------------


def test_a_second_apply_of_an_unchanged_bundle_writes_nothing_at_all(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    sync.apply(v, scope)
    v.calls.clear()
    out = sync.apply(v, scope)
    assert out["skipped"] is True
    assert [c for c in v.calls if c in ("create", "update", "delete",
                                        "place", "attach", "ensure")] == []


def test_force_bypasses_the_cursor(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    sync.apply(v, scope)
    assert "skipped" not in sync.apply(v, scope, force=True)


def test_the_cursor_is_not_written_when_a_pass_dies_partway(scope):
    """A pass that died halfway must retry rather than declare itself done."""
    seed(scope, [item("AAA")])
    v = FakeVault()
    boom = RuntimeError("the vault went away")

    def explode(*a, **k):
        raise boom
    v.place = explode
    with pytest.raises(RuntimeError):
        sync.apply(v, scope)
    roots = [n for n in v.notes.values() if sync.ROOT_LABEL in n["labels"]]
    assert sync.APPLIED_LABEL not in roots[0]["labels"]


def test_a_dry_run_reports_what_would_change_and_writes_nothing(scope):
    seed(scope, [item("AAA"), item("BBB")])
    v = FakeVault()
    out = sync.apply(v, scope, dry_run=True)
    assert out["would_create"] == 2 and out["would_delete"] == 0
    assert out["would_visit"] == 2
    assert v.notes == {}


def test_a_dry_run_on_a_node_that_may_not_create_the_root_refuses(scope):
    seed(scope, [item("AAA")])
    with pytest.raises(sync.NoRoot):
        sync.apply(FakeVault(), scope, dry_run=True, may_create=False)


def test_a_dry_run_after_an_apply_says_it_is_up_to_date(scope):
    seed(scope, [item("AAA")])
    v = FakeVault()
    sync.apply(v, scope)
    out = sync.apply(v, scope, dry_run=True)
    assert out["up_to_date"] is True
    assert out["would_create"] == 0 and out["would_delete"] == 0


# -- shipping ----------------------------------------------------------------


def test_ship_sends_only_the_library_json_through_a_sudo_rsync(scope, monkeypatch):
    seed(scope, [item("AAA")])
    seen = []

    def fake_run(argv, *, stdin=None):
        seen.append((argv, stdin))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(sync, "_run", fake_run)

    out = sync.ship("sirius:/var/lib/awm/projects/vault/main", scope)
    argv = seen[-1][0]
    assert argv[0] == "rsync"
    assert "--rsync-path=sudo -n -u awm rsync" in argv
    assert argv[-2].endswith("library.json")
    assert argv[-1] == "sirius:/var/lib/awm/projects/vault/main/data/zotero/"
    assert not any("files" in a for a in argv)
    assert out["shipped"] is True


def test_ship_carries_a_writable_mode_over_the_read_only_hardlink(scope, monkeypatch):
    """library.json is a read-only hardlink into the DVC cache, and `-a` would
    leave the far node a bundle its own pull could never replace."""
    seed(scope, [item("AAA")])
    monkeypatch.setattr(sync, "_run", lambda argv, *, stdin=None:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    argv = sync._rsync_argv(bundle.Bundle(scope).library_json, "h", "/d")
    assert "--chmod=F644" in argv


def test_ship_feeds_the_remote_shell_on_stdin_rather_than_as_an_argument(scope,
                                                                        monkeypatch):
    """`sudo -u` runs one command, so a `&&` written in an argument string
    would belong to the calling user's shell."""
    seed(scope, [item("AAA")])
    seen = []
    monkeypatch.setattr(sync, "_run", lambda argv, *, stdin=None:
                        (seen.append((argv, stdin)),
                         subprocess.CompletedProcess(argv, 0, "", ""))[1])
    sync.ship("sirius:/vault", scope)
    argv, stdin = seen[0]
    assert argv == ["ssh", "sirius", "sudo -n -u awm bash -s"]
    assert "&&" in stdin and "mkdir -p /vault/data/zotero" in stdin


def test_ship_holds_the_lock(scope):
    """The bundle is written by truncate-then-write, so a courier that did not
    take the lock could read a torn file."""
    seed(scope, [item("AAA")])
    with sync.exclusive(scope):
        with pytest.raises(sync.Busy):
            sync.ship("sirius:/vault", scope)


def test_a_destination_with_no_path_is_refused(scope):
    with pytest.raises(ValueError, match="host:/path"):
        sync.ship("sirius", scope)


def test_a_failed_ship_is_reported_rather_than_raised(scope, monkeypatch):
    """The far node being asleep is ordinary, and it leaves that node serving
    the last good mirror rather than none."""
    seed(scope, [item("AAA")])
    monkeypatch.setattr(sync, "_pull", lambda b, **k: {"changed": True})
    monkeypatch.setattr(sync, "_run", lambda argv, *, stdin=None:
                        subprocess.CompletedProcess(argv, 255, "", "no route"))
    out = sync.run(FakeVault(), scope, apply_here=False,
                   ship_to="sirius:/vault")
    assert out["ship"]["shipped"] is False
    assert "no route" in out["ship"]["detail"]
