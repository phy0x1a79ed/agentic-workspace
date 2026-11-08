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

from .fakes import FakeLibrary, MemoryStore

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def scope():
    """Where the bundle lives. A variable: these tests are about the read."""
    return MemoryStore()


@pytest.fixture
def library(monkeypatch):
    """One personal library and one group, in front of the real client."""
    return FakeLibrary({"users/0": "My Library",
                        "groups/1": "A group"}).install(monkeypatch, sync)


def caught_up(scope, library) -> bundle.Bundle:
    """A bundle that has read every library whole, just now."""
    b = bundle.Bundle(scope)
    sync._pull(b, force=True)
    return b


# -- what a pass costs -------------------------------------------------------


def test_a_push_reads_only_the_library_it_was_told_about(scope, library):
    library.put("AAA", title="Mine")
    library.put("GGG", library="groups/1", title="Theirs")
    b = caught_up(scope, library)
    library.calls.clear()
    library.put("BBB", title="Just saved")

    sync._pull(b, force=False, only=["users/0"])

    assert {c[1] for c in library.asked("items")} == {"users/0"}


def test_the_floor_reads_the_deletions_the_hot_path_skipped(scope, library):
    library.put("AAA", title="A paper")
    library.put("BBB", title="Another")
    b = caught_up(scope, library)
    library.erase("BBB")

    sync._pull(b, force=False)

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

    out = sync._pull(b, force=False)

    assert "My Library" in out["whole"], "the pass did not escalate"
    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA"}


# -- the ways this could empty the vault -------------------------------------


def test_a_library_that_reports_a_lower_version_is_refused(scope, library):
    """A reader that has fallen behind is not a library that has changed.
    Reading from it rewrites the bundle backwards, and the removal pass then
    retracts every paper the bundle had and the reader did not."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    library.version["users/0"] = 0

    out = sync._pull(b, force=False)

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

    out = sync._pull(b, force=False)

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
        sync._pull(b, force=True)

    assert {i["ref"] for i in b.read()["items"]} == {"users/0/AAA"}


def test_the_whole_read_stamp_stays_out_of_the_digest(scope, library):
    """A timestamp inside the digested keys would make every node re-apply the
    whole mirror to prove nothing had changed."""
    library.put("AAA", title="A paper")
    b = caught_up(scope, library)
    was = b.digest

    payload = json.loads(scope.blob)
    assert payload["whole_read"], "the stamp was not recorded"
    payload["whole_read"]["users/0"] = "1999-01-01T00:00:00Z"
    scope.save(json.dumps(payload).encode())

    assert bundle.Bundle(scope).digest == was


# -- the one that matters ----------------------------------------------------


def test_replaying_windows_lands_where_one_whole_read_would(scope, library):
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
        sync._pull(delta, force=False)
        whole = bundle.Bundle(MemoryStore())
        sync._pull(whole, force=True)
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
