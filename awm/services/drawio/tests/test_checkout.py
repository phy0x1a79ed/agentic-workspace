"""The concurrency contract: two writers, one diagram, no silent loss.

The scenarios here are the ones that actually happened in the prototype and
cost real work — a stale tab reverting a scripted rebuild, an agent's edits
overwritten mid-session. Each is expressed as an assertion that the outcome is
now either *both changes present* or *a loud refusal*, never a plausible-looking
wrong document.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from awm.drawio import xmlmodel
from awm.drawio.checkout import Behind, Checkouts, Conflicted
from awm.drawio.store import Store, StoreError, UnknownDiagram

TEMPLATE = """<mxfile>
  <diagram id="p1" name="Page-1">
    <mxGraphModel grid="1" pageWidth="850">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="a" value="A" vertex="1" parent="1">
          <mxGeometry x="0" y="0" width="80" height="40" as="geometry" />
        </mxCell>
        <mxCell id="b" value="B" vertex="1" parent="1">
          <mxGeometry x="0" y="200" width="80" height="40" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "diagrams")


@pytest.fixture()
def checkouts(store, tmp_path):
    return Checkouts(store, tmp_path / "checkouts")


@pytest.fixture()
def diagram(store):
    store.create("fig/demo.drawio", author="tester", xml=TEMPLATE)
    return "fig/demo.drawio"


def set_value(text: str, cell_id: str, value: str) -> str:
    """Edit one cell's label, the way an operation would."""
    root = ET.fromstring(text)
    root.find(f".//mxCell[@id='{cell_id}']").set("value", value)
    return ET.tostring(root, encoding="unicode")


def move(text: str, cell_id: str, x: float) -> str:
    root = ET.fromstring(text)
    root.find(f".//mxCell[@id='{cell_id}']/mxGeometry").set("x", str(x))
    return ET.tostring(root, encoding="unicode")


# --- store -----------------------------------------------------------------

def test_create_then_read(store, diagram):
    assert store.exists(diagram)
    assert xmlmodel.parse(store.read(diagram)) is not None


def test_paths_are_canonicalized(store):
    store.create("a/b/c", author="t", xml=TEMPLATE)
    assert store.exists("a/b/c.drawio")
    assert [d["save"] for d in store.list()] == ["a/b/c.drawio"]


def test_path_escape_is_refused(store):
    for bad in ("../outside", "/etc/passwd.drawio", "a/../../b", ".git/x"):
        with pytest.raises(StoreError):
            store.create(bad, author="t", xml=TEMPLATE)


def test_scrolling_does_not_create_a_revision(store, diagram):
    """A tab that is scrolled but not edited re-saves identical canonical bytes."""
    before = store.head_rev(diagram)
    scrolled = store.read(diagram).replace(
        "<mxGraphModel", '<mxGraphModel dx="9999" dy="8888"')
    result = store.write(diagram, scrolled, author="browser")
    assert result["changed"] is False
    assert store.head_rev(diagram) == before


def test_editing_burst_folds_into_one_revision(store, diagram):
    for i in range(5):
        store.write(diagram, set_value(store.read(diagram), "a", f"v{i}"),
                    author="browser")
    # create + one folded burst
    assert len(store.history(diagram)) == 2
    assert "v4" in store.read(diagram)


def test_different_authors_do_not_fold(store, diagram):
    store.write(diagram, set_value(store.read(diagram), "a", "x"), author="browser")
    store.write(diagram, set_value(store.read(diagram), "a", "y"), author="agent")
    assert len(store.history(diagram)) == 3


def test_history_and_restore(store, diagram):
    store.write(diagram, set_value(store.read(diagram), "a", "changed"),
                author="agent", label="rename A")
    original = store.history(diagram)[-1].rev

    store.restore(diagram, original, author="tester")
    assert 'value="A"' in store.read(diagram)
    # Forward-only: the change is still in history, not rewritten away.
    assert any(r.label == "rename A" for r in store.history(diagram))


def test_history_carries_author_and_label(store, diagram):
    store.write(diagram, set_value(store.read(diagram), "a", "z"),
                author="agent-7", label="place molecule")
    tip = store.history(diagram)[0]
    assert tip.author == "agent-7" and tip.label == "place molecule"


def test_diagrams_do_not_block_each_other(store):
    """Per-path drift is what keeps a shared repo from coupling diagrams."""
    store.create("one.drawio", author="t", xml=TEMPLATE)
    store.create("two.drawio", author="t", xml=TEMPLATE)
    base = store.head_rev("one.drawio")
    store.write("two.drawio", set_value(store.read("two.drawio"), "a", "x"),
                author="other")
    assert store.changed_since("one.drawio", base) == []


def test_unknown_diagram_is_refused(store):
    with pytest.raises(UnknownDiagram):
        store.read("nope.drawio")


def test_compressed_content_is_never_committed(store, diagram):
    with pytest.raises(ValueError):
        store.write(diagram, "<mxfile><diagram id='p'>H4sIAAA=</diagram></mxfile>",
                    author="browser")


# --- checkout: the happy path ---------------------------------------------

def test_checkout_edit_merge(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(set_value(path.read_text(), "a", "by agent"), encoding="utf-8")

    result = checkouts.merge(handle.id)
    assert result["changed"] is True
    assert 'value="by agent"' in store.read(diagram)
    assert checkouts.list(diagram) == []


def test_merge_is_a_no_op_when_nothing_changed(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    assert checkouts.merge(handle.id)["changed"] is False


def test_status_reports_ahead_and_behind(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    assert checkouts.status(handle.id)["ahead"] is False

    path = checkouts.file_path(handle.id)
    path.write_text(set_value(path.read_text(), "a", "agent"), encoding="utf-8")
    store.write(diagram, set_value(store.read(diagram), "b", "user"), author="browser")

    status = checkouts.status(handle.id)
    assert status["ahead"] is True and status["behind"] == 1


# --- checkout: concurrent editing -----------------------------------------

def test_disjoint_concurrent_edits_both_survive(store, checkouts, diagram):
    """The motivating case: agent on one cell, person on another."""
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(set_value(path.read_text(), "a", "agent-made-this"),
                    encoding="utf-8")

    store.write(diagram, set_value(store.read(diagram), "b", "user-made-this"),
                author="browser")

    assert checkouts.update(handle.id)["conflicts"] == 0
    checkouts.merge(handle.id)

    landed = store.read(diagram)
    assert 'value="agent-made-this"' in landed
    assert 'value="user-made-this"' in landed


def test_merge_refuses_while_behind(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(set_value(path.read_text(), "a", "agent"), encoding="utf-8")
    store.write(diagram, set_value(store.read(diagram), "b", "user"), author="browser")

    with pytest.raises(Behind):
        checkouts.merge(handle.id)


def test_stale_writer_cannot_revert_the_live_diagram(store, checkouts, diagram):
    """The prototype's worst failure: a stale snapshot silently reverting work.

    The agent holds a copy from before the user's change and never updates.
    Landing it would erase the user's edit, so the attempt is refused outright.
    """
    handle = checkouts.checkout(diagram, author="agent")
    store.write(diagram, set_value(store.read(diagram), "b", "user work"),
                author="browser")

    with pytest.raises(Behind):
        checkouts.merge(handle.id)
    assert 'value="user work"' in store.read(diagram)


def test_true_conflict_is_reported_not_resolved(store, checkouts, diagram):
    """Both sides move the same cell. Guessing here is the failure mode."""
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(move(path.read_text(), "a", 500), encoding="utf-8")
    store.write(diagram, move(store.read(diagram), "a", 900), author="browser")

    result = checkouts.update(handle.id)
    assert result["conflicts"] == 1
    assert "<<<<<<<" in path.read_text()

    with pytest.raises(Conflicted):
        checkouts.merge(handle.id)


def test_manual_resolution_is_the_escape_hatch(store, checkouts, diagram):
    """v1 will meet cases the operation layer cannot express. Hand-editing the
    checkout must always be a way out."""
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(move(path.read_text(), "a", 500), encoding="utf-8")
    store.write(diagram, move(store.read(diagram), "a", 900), author="browser")
    checkouts.update(handle.id)

    # The agent reads the marked-up file, decides, and writes it back by hand.
    resolved = "\n".join(
        line for line in path.read_text().splitlines()
        if not line.startswith(("<<<<<<<", "=======", ">>>>>>>"))
        and 'x="900"' not in line
    )
    path.write_text(resolved + "\n", encoding="utf-8")

    assert checkouts.resolve(handle.id)["state"] == "clean"
    checkouts.merge(handle.id)
    assert 'x="500"' in store.read(diagram)


def test_resolve_refuses_leftover_markers(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(move(path.read_text(), "a", 500), encoding="utf-8")
    store.write(diagram, move(store.read(diagram), "a", 900), author="browser")
    checkouts.update(handle.id)

    with pytest.raises(Conflicted):
        checkouts.resolve(handle.id)


def test_resolve_refuses_a_broken_document(store, checkouts, diagram):
    """Hand-editing is trusted for content, not for producing valid XML."""
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(move(path.read_text(), "a", 500), encoding="utf-8")
    store.write(diagram, move(store.read(diagram), "a", 900), author="browser")
    checkouts.update(handle.id)
    path.write_text("<mxfile><diagram>oops", encoding="utf-8")

    with pytest.raises(Exception):
        checkouts.resolve(handle.id)


def test_update_refuses_while_conflicted(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(move(path.read_text(), "a", 500), encoding="utf-8")
    store.write(diagram, move(store.read(diagram), "a", 900), author="browser")
    checkouts.update(handle.id)

    with pytest.raises(Conflicted):
        checkouts.update(handle.id)


def test_update_when_already_current_is_a_no_op(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    assert checkouts.update(handle.id)["updated"] is False


def test_long_lived_checkout_survives_many_live_revisions(store, checkouts, diagram):
    """An agent iterating for a long time against an actively-edited diagram."""
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(set_value(path.read_text(), "a", "agent v1"), encoding="utf-8")

    for i in range(5):
        store.write(diagram, set_value(store.read(diagram), "b", f"user {i}"),
                    author=f"browser-{i}")
        assert checkouts.update(handle.id)["conflicts"] == 0
        path.write_text(set_value(path.read_text(), "a", f"agent v{i + 2}"),
                        encoding="utf-8")

    checkouts.merge(handle.id)
    landed = store.read(diagram)
    assert 'value="agent v6"' in landed and 'value="user 4"' in landed


# --- checkout: bookkeeping -------------------------------------------------

def test_base_revision_is_protected_from_amend(store, checkouts, diagram):
    """Amending a commit a checkout is based on would move its base sha."""
    handle = checkouts.checkout(diagram, author="browser")
    base = handle.base_rev
    store.write(diagram, set_value(store.read(diagram), "b", "more"),
                author="browser")
    assert store.read(diagram, rev=base) is not None
    assert store.head_rev(diagram) != base


def test_discard_removes_the_working_copy(store, checkouts, diagram):
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    checkouts.discard(handle.id)
    assert not path.exists()
    assert checkouts.list() == []


def test_checkouts_are_listed_per_diagram(store, checkouts, diagram):
    store.create("other.drawio", author="t", xml=TEMPLATE)
    checkouts.checkout(diagram, author="a1")
    checkouts.checkout(diagram, author="a2")
    checkouts.checkout("other.drawio", author="a3")
    assert len(checkouts.list(diagram)) == 2
    assert len(checkouts.list()) == 3


def test_registry_survives_a_restart(store, checkouts, diagram, tmp_path):
    handle = checkouts.checkout(diagram, author="agent")
    path = checkouts.file_path(handle.id)
    path.write_text(set_value(path.read_text(), "a", "survived"), encoding="utf-8")

    revived = Checkouts(Store(store.root), tmp_path / "checkouts")
    assert [h.id for h in revived.list()] == [handle.id]
    revived.merge(handle.id)
    assert 'value="survived"' in store.read(diagram)


def test_checkout_of_missing_diagram_is_refused(checkouts):
    with pytest.raises(Exception):
        checkouts.checkout("ghost.drawio", author="agent")
