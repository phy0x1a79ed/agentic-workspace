"""Reading only what moved, without ever telling the vault that the read was
partial.

The bundle stays a full picture of the library. Everything here is about the
seam that keeps that true, because the pass that retires a paper does it by
absence from the bundle — so a read that comes back short in the wrong way
deletes somebody's notes with every call succeeding.
"""

from __future__ import annotations

import json

import pytest

from awm.zotero import bundle, source, sync

from .fakes import FakeLibrary

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def scope(tmp_path):
    return tmp_path


@pytest.fixture
def library(monkeypatch):
    """One personal library and one group, in front of the real client."""
    return FakeLibrary({"users/0": "My Library",
                        "groups/1": "A group"}).install(monkeypatch, sync)


def caught_up(scope, library) -> bundle.Bundle:
    """A bundle that has read every library whole, just now."""
    b = bundle.Bundle(scope)
    sync._pull(b, force=True, commit=False)
    return b


# -- what a pass costs -------------------------------------------------------


def test_a_tick_where_nothing_moved_asks_once_per_library(scope, library):
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    library.calls.clear()

    out = sync._pull(b, force=False, commit=False)

    assert out["changed"] is False
    assert len(library.asked("items")) == 2, "one window read per library"
    assert library.asked("library_version") == [], \
        "the window read is the movement probe"


def test_a_paper_lands_in_one_request(scope, library):
    """The whole point. Somebody is standing over this one."""
    library.put("AAA", title="First")
    b = caught_up(scope, library)
    library.calls.clear()
    library.put("BBB", title="Just saved")

    out = sync._pull(b, force=False, commit=False, only=["users/0"])

    assert out["changed"] is True
    # One request between the stream frame and the paper. Not the library list,
    # not a version probe, not the collections window, not the deletions.
    assert library.calls == [("items", "users/0", 1)], library.calls
    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA",
                                                    "users/0/BBB"}


def test_a_push_reads_only_the_library_it_was_told_about(scope, library):
    library.put("AAA", title="Mine")
    library.put("GGG", library="groups/1", title="Theirs")
    b = caught_up(scope, library)
    library.calls.clear()
    library.put("BBB", title="Just saved")

    sync._pull(b, force=False, commit=False, only=["users/0"])

    assert {c[1] for c in library.asked("items")} == {"users/0"}


def test_the_floor_reads_the_deletions_the_hot_path_skipped(scope, library):
    library.put("AAA", title="A paper")
    library.put("BBB", title="Another")
    b = caught_up(scope, library)
    library.erase("BBB")

    sync._pull(b, force=False, commit=False)

    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA"}


# -- what a window cannot see ------------------------------------------------


def test_a_trashed_paper_makes_the_pass_read_the_library_whole(scope, library):
    """A paper in the trash is absent from `/items`, absent from a window and
    absent from the deletions route. The library's version moving with nothing
    to show for it is the only sign, and only absence from a whole read
    retracts it."""
    library.put("AAA", title="A paper")
    library.put("BBB", title="Doomed")
    b = caught_up(scope, library)
    library.calls.clear()
    library.trash("BBB")

    out = sync._pull(b, force=False, commit=False)

    assert "My Library" in out["whole"], "the pass did not escalate"
    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA"}


def test_a_collection_rename_does_not_cost_a_whole_read(scope, library):
    """It moves the version and returns no items, which looks exactly like a
    trashing until the collections window is read. Escalating on it would spend
    the eleven-page read this whole change exists to avoid."""
    library.put_collection("COL", "Papers")
    library.put("AAA", title="A paper", collections=["COL"])
    b = caught_up(scope, library)
    library.calls.clear()
    library.put_collection("COL", "Renamed")

    out = sync._pull(b, force=False, commit=False)

    assert out["whole"] == [], "a rename should not read the library whole"
    assert [c["name"] for c in b.read()["collections"]] == ["Renamed"]


def test_a_library_unread_for_a_day_is_read_whole(scope, library, monkeypatch):
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    monkeypatch.setattr(sync, "_stale", lambda when: True)
    library.calls.clear()
    library.put("BBB", title="Another")

    out = sync._pull(b, force=False, commit=False)

    assert "My Library" in out["whole"]
    assert [c[2] for c in library.asked("items")] == [None, None], \
        "a whole read must send no cursor"


def test_forcing_sends_no_cursor(scope, library):
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    library.calls.clear()

    sync._pull(b, force=True, commit=False)

    assert [c[2] for c in library.asked("items")] == [None, None]


# -- the ways this could empty the vault -------------------------------------


def test_a_library_that_reports_a_lower_version_is_refused(scope, library):
    """A reader that has fallen behind is not a library that has changed.
    Reading from it rewrites the bundle backwards, and the removal pass then
    retracts every paper the bundle had and the reader did not."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    library.version["users/0"] = 0

    out = sync._pull(b, force=False, commit=False)

    assert out["behind"] == ["users/0"]
    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA"}


def test_a_library_whose_read_fails_keeps_its_papers(scope, library):
    """Not read is not the same as read and found empty."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)

    def _boom(lib=None, since=None):
        raise source.ZoteroError("refused")

    library.items = _boom
    import awm.zotero.sync as module
    module.source.items = _boom

    out = sync._pull(b, force=False, commit=False)

    assert out["unread"] == ["users/0", "groups/1"]
    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA"}


def test_a_whole_read_that_comes_back_empty_is_refused(scope, library):
    """The one shape that empties the vault in a single pass with every call
    succeeding."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    library.items_by["users/0"].clear()
    library.version["users/0"] += 1

    with pytest.raises(sync.ZoteroLostItsLibrary):
        sync._pull(b, force=True, commit=False)

    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA"}


def test_the_whole_read_stamp_stays_out_of_the_digest(scope, library):
    """A timestamp inside the digested keys would make every node re-apply the
    whole mirror to prove nothing had changed."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    was = b.digest

    payload = json.loads(b.library_json.read_text())
    assert payload["whole_read"], "the stamp was not recorded"
    payload["whole_read"]["users/0"] = "1999-01-01T00:00:00Z"
    b.library_json.write_text(json.dumps(payload))

    assert b.digest == was


def test_a_narrowed_pass_asks_nobody_which_libraries_exist(scope, library):
    """On a push the caller was told which library moved, and the bundle already
    carries that library's name on every record of it. Asking Zotero is a
    request spent learning what is already known."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    library.calls.clear()
    library.put("BBB", title="Just saved")

    sync._pull(b, force=False, commit=False, only=["users/0"])

    assert library.asked("libraries") == []


def test_a_library_the_bundle_has_never_seen_is_worth_a_request(scope, library):
    """A name is inside every fingerprint and is the shelf's title, so an empty
    one rewrites a library's worth of notes and renames their shelf to
    nothing."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    library.calls.clear()
    library.put("GGG", library="groups/1", title="First in the group")

    out = sync._pull(b, force=False, commit=False, only=["groups/1"])

    assert library.asked("libraries"), "the name was guessed rather than asked"
    assert out["read"] == ["A group"]


# -- the one that matters ----------------------------------------------------


def test_replaying_windows_lands_where_one_whole_read_would(scope, library,
                                                            tmp_path):
    """The property the whole design rests on, and the only test that keeps
    biting as the fold is refactored.

    Drive a library through the operations a person performs. After every one,
    catch up by window and compare against a bundle built by reading the same
    state whole. The two must be the same library, because everything downstream
    treats the bundle as a full picture and cannot tell which way it got there.

    Compared after *every* step rather than at the end, because an escalation to
    a whole read rebuilds every record and would heal a divergence introduced
    three steps earlier. Checking only the end result hides exactly the bugs this
    is here to catch.
    """
    delta = bundle.Bundle(scope)
    step = 0

    def catch_up(what: str):
        nonlocal step
        step += 1
        sync._pull(delta, force=False, commit=False)
        whole = bundle.Bundle(tmp_path / f"whole{step}")
        sync._pull(whole, force=True, commit=False)
        assert delta.read()["items"] == whole.read()["items"], what
        assert delta.read()["collections"] == whole.read()["collections"], what
        assert delta.digest == whole.digest, what

    library.put_collection("COL", "Papers")
    library.put("AAA", title="First", collections=["COL"])
    library.put("BBB", title="Second")
    library.put("GGG", library="groups/1", title="Shared")
    catch_up("the first read")

    library.put("N1", parent="AAA", item_type="note", note="<p>one</p>")
    catch_up("a note added to a paper nothing else touched")

    library.put("AAA", title="First, corrected", collections=["COL"],
                tags=[{"tag": "b"}, {"tag": "a"}])
    catch_up("a paper edited, whose notes did not come with it")

    library.put("N2", parent="AAA", item_type="note", note="<p>two</p>")
    catch_up("a second note on the same paper")

    library.put("N1", parent="AAA", item_type="note", note="<p>one, edited</p>")
    catch_up("one note of two edited")

    library.put("CCC", title="Third", collections=["COL"])
    catch_up("another paper")

    library.put_collection("COL", "Papers, renamed")
    catch_up("a collection renamed, which moves the version and returns no items")

    library.erase("BBB")
    catch_up("a paper deleted for good")

    library.erase("N2")
    catch_up("a child note deleted for good, whose key names no paper")

    library.trash("CCC")
    catch_up("a paper trashed, which no route reports")


def test_a_group_renamed_outside_its_version_waits_for_a_whole_read(scope,
                                                                    library):
    """Not a defect, and worth stating because the equivalence test above leaves
    it out. A library's display name is not part of its contents, so renaming a
    group moves no version and no window can carry it. The daily whole read is
    what picks it up."""
    library.put("GGG", library="groups/1", title="Shared")
    b = caught_up(scope, library)
    library.rename_library("groups/1", "Renamed group")

    sync._pull(b, force=False, commit=False)
    assert {i["library_name"] for i in b.read()["items"]} == {"A group"}

    sync._pull(b, force=True, commit=False)
    assert {i["library_name"] for i in b.read()["items"]} == {"Renamed group"}
