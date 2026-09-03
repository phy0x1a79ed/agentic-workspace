"""Reading and writing the vault from outside Trilium's own UI.

`note_upsert` could only create-or-replace by title, which is enough to seed a
demo note and not enough to place a card, label a paper or attach a PDF. These
tests cover the surface that replaced it, and in particular the three places
where the ETAPI shape underneath is not the shape a caller would guess:
attributes are not part of creating a note, a note's parent is a branch rather
than a field, and an attachment's bytes do not travel in its JSON.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.trilium import etapi, hub_adapter

from .fake_vault import FakeVault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def _never_the_real_vault(monkeypatch):
    """No test in this file may reach a running Trilium.

    Not a precaution. An earlier version of the empty-column test called
    `board.ensure` without a fake, the call fell through to the default
    columns instead of being refused, and it created a real board in the live
    vault on this host. The `vault` fixture is opt-in per test and overrides
    this one; being unable to reach a socket is not something to remember.
    """
    def _refuse(*a, **kw):
        raise AssertionError(
            "a test reached the live vault — use the `vault` fixture")
    monkeypatch.setattr(etapi.httpx, "request", _refuse)


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    return fake


def call(verb: str, **args):
    """A verb as the console reaches it: `as_` is None, so the gate admits."""
    return asyncio.run(hub_adapter.HANDLERS[verb](args, None))


# -- creating ----------------------------------------------------------------


def test_a_note_is_created_with_its_labels_in_one_call(vault):
    """ETAPI's create-note whitelist takes no attributes, so this is three
    round trips wearing one verb. Doing it here is the point: every caller
    would otherwise write the loop, and half would forget the relation."""
    out = call("note_create", title="Paper", content="<p>abstract</p>",
               labels={"year": "2019", "doi": "10.1/x"},
               relations={"cites": "root"})
    assert out["created"] is True
    assert vault.labels(out["note_id"]) == {"year": "2019", "doi": "10.1/x"}
    assert [a for a in vault.attributes.values()
            if a["type"] == "relation"][0]["value"] == "root"


def test_labels_arrive_as_json_when_the_surface_has_no_object_type(vault):
    """One catalog projects each verb onto MCP, HTTP and the CLI, and the CLI
    maps `object` to `str`. Both spellings have to be the same verb."""
    out = call("note_create", title="Paper", labels='{"year": "2019"}')
    assert vault.labels(out["note_id"]) == {"year": "2019"}


def test_a_label_written_with_its_sigil_is_stored_without_one(vault):
    out = call("note_create", title="Paper", labels={"#year": "2019"},
               relations={"~cites": "root"})
    assert "year" in vault.labels(out["note_id"])


def test_labels_that_are_not_an_object_are_refused(vault):
    with pytest.raises(ValueError, match="object"):
        call("note_create", title="Paper", labels="[1, 2]")
    with pytest.raises(ValueError, match="not JSON"):
        call("note_create", title="Paper", labels="year=2019")


def test_note_create_always_makes_a_new_one(vault):
    """The difference from note_upsert, which is what makes it usable when the
    title is not the identity — two papers may share a title."""
    first = call("note_create", title="Paper")
    second = call("note_create", title="Paper")
    assert first["note_id"] != second["note_id"]


# -- reading -----------------------------------------------------------------


def test_note_get_returns_the_body_and_the_attachments(vault):
    made = call("note_create", title="Paper", content="<p>x</p>")
    call("attachment_put", note_id=made["note_id"], title="p.pdf",
         content_b64="aGVsbG8=")
    got = call("note_get", note_id=made["note_id"])
    assert got["content"] == "<p>x</p>"
    assert [a["title"] for a in got["attachments"]] == ["p.pdf"]


def test_note_get_can_leave_the_body_out(vault):
    made = call("note_create", title="Paper", content="<p>x</p>")
    assert "content" not in call("note_get", note_id=made["note_id"],
                                 content=False)


def test_note_children_reports_the_tree_without_the_bodies(vault):
    parent = call("note_create", title="Folder")["note_id"]
    call("note_create", title="One", parent=parent)
    out = call("note_children", note_id=parent)
    assert out["count"] == 1
    assert out["children"][0]["title"] == "One"
    assert "content" not in out["children"][0]


def test_attrs_get_says_which_note_owns_each_attribute(vault):
    """An inherited label belongs to whatever declared it, and rewriting it
    there would change every note sharing that template."""
    made = call("note_create", title="Paper", labels={"year": "2019"})
    out = call("attrs_get", note_id=made["note_id"])
    assert out["attributes"][0] == {
        "attribute_id": out["attributes"][0]["attribute_id"], "type": "label",
        "name": "year", "value": "2019", "inheritable": False, "owned": True}


# -- changing ----------------------------------------------------------------


def test_note_update_touches_only_what_it_is_given(vault):
    made = call("note_create", title="Paper", content="<p>x</p>")["note_id"]
    call("note_update", note_id=made, title="Better paper")
    assert vault.notes[made]["title"] == "Better paper"
    assert vault.notes[made]["content"] == "<p>x</p>"


def test_note_update_says_when_the_body_was_already_right(vault):
    """A periodic sync calls this on everything it knows; rewriting an
    unchanged body would put a revision on every note every pass."""
    made = call("note_create", title="Paper", content="<p>x</p>")["note_id"]
    assert call("note_update", note_id=made,
                content="<p>x</p>")["changed"]["content"] is False
    assert call("note_update", note_id=made,
                content="<p>y</p>")["changed"]["content"] is True


def test_note_update_with_nothing_to_change_is_a_refusal(vault):
    made = call("note_create", title="Paper")["note_id"]
    with pytest.raises(ValueError, match="nothing to change"):
        call("note_update", note_id=made)


def test_a_label_is_set_once_however_many_times_it_is_written(vault):
    made = call("note_create", title="Paper")["note_id"]
    first = call("attr_set", note_id=made, name="status", value="Doing")
    again = call("attr_set", note_id=made, name="status", value="Doing")
    assert again == {"attribute_id": first["attribute_id"], "created": False,
                     "changed": False}
    assert len([a for a in vault.attributes.values()
                if a["name"] == "status"]) == 1


def test_a_relation_is_replaced_rather_than_patched(vault):
    """Upstream will not patch a relation's target — only its position — so a
    changed target has to be a new row."""
    made = call("note_create", title="Paper")["note_id"]
    other = call("note_create", title="Other")["note_id"]
    first = call("attr_set", note_id=made, name="cites", value="root",
                 type="relation")
    moved = call("attr_set", note_id=made, name="cites", value=other,
                 type="relation")
    assert moved["attribute_id"] != first["attribute_id"]
    assert moved["changed"] is True
    assert len([a for a in vault.attributes.values()
                if a["name"] == "cites"]) == 1


def test_attr_delete_removes_every_copy_the_note_owns(vault):
    made = call("note_create", title="Paper", labels={"tag": "a"})["note_id"]
    assert call("attr_delete", note_id=made, name="tag")["removed"] == 1
    assert vault.labels(made) == {}


# -- placement ---------------------------------------------------------------


def test_moving_a_note_adds_the_new_parent_before_dropping_the_old(vault):
    """A note's last branch takes the note with it, so the other order would
    delete what it was asked to move."""
    home = call("note_create", title="Home")["note_id"]
    away = call("note_create", title="Away")["note_id"]
    moved = call("note_create", title="Paper", parent=home)["note_id"]
    call("note_move", note_id=moved, parent=away)
    assert vault.notes[moved]["parents"] == [away]
    assert moved in vault.notes[away]["children"]
    assert moved not in vault.notes[home]["children"]


def test_cloning_leaves_the_note_in_both_places(vault):
    """One note in two places, not a copy — which is how a paper in three
    Zotero collections stays one note."""
    home = call("note_create", title="Home")["note_id"]
    away = call("note_create", title="Away")["note_id"]
    paper = call("note_create", title="Paper", parent=home)["note_id"]
    call("note_clone", note_id=paper, parent=away)
    assert sorted(vault.notes[paper]["parents"]) == sorted([home, away])


def test_deleting_a_note_takes_its_subtree(vault):
    parent = call("note_create", title="Folder")["note_id"]
    child = call("note_create", title="One", parent=parent)["note_id"]
    call("note_delete", note_id=parent)
    assert parent not in vault.notes and child not in vault.notes


def test_deleting_root_is_refused(vault):
    with pytest.raises(ValueError, match="root is the vault"):
        call("note_delete", note_id="root")


# -- attachments -------------------------------------------------------------


def test_an_attachment_carries_its_bytes_in_a_second_call(vault):
    """ETAPI validates the create body's `content` as a string, which a PDF is
    not — so the row is made empty and the bytes are PUT after it."""
    made = call("note_create", title="Paper")["note_id"]
    out = call("attachment_put", note_id=made, title="p.pdf",
               content_b64="JVBERi0=")
    assert out["created"] is True and out["bytes"] == 5
    assert vault.attachments[out["attachment_id"]]["blob"] == b"%PDF-"


def test_an_attachment_from_a_path_is_named_and_typed_by_the_file(vault, tmp_path):
    src = tmp_path / "Gruber - nitrogen.pdf"
    src.write_bytes(b"%PDF-1.4")
    out = call("attachment_put", note_id=call("note_create",
                                              title="Paper")["note_id"],
               path=str(src))
    assert out["title"] == "Gruber - nitrogen.pdf"
    assert out["mime"] == "application/pdf"


def test_re_attaching_the_same_file_does_not_re_upload_it(vault, tmp_path):
    """A periodic sync passes over every paper; re-sending a 20 MB PDF each
    time is the difference between a free tick and a slow one."""
    src = tmp_path / "p.pdf"
    src.write_bytes(b"%PDF-1.4")
    note = call("note_create", title="Paper")["note_id"]
    call("attachment_put", note_id=note, path=str(src))
    vault.calls.clear()
    again = call("attachment_put", note_id=note, path=str(src))
    assert again["changed"] is False
    assert not [c for c in vault.calls if c[0] in ("POST", "PUT")]


def test_a_changed_file_replaces_the_attachment_in_place(vault, tmp_path):
    src = tmp_path / "p.pdf"
    src.write_bytes(b"%PDF-1.4")
    note = call("note_create", title="Paper")["note_id"]
    first = call("attachment_put", note_id=note, path=str(src))
    src.write_bytes(b"%PDF-1.4 and more")
    again = call("attachment_put", note_id=note, path=str(src))
    assert again["attachment_id"] == first["attachment_id"]
    assert again["changed"] is True
    assert len(vault.attachments) == 1


def test_bytes_and_a_path_together_are_a_refusal(vault):
    note = call("note_create", title="Paper")["note_id"]
    for args in ({}, {"path": "/tmp/x", "content_b64": "eA=="}):
        with pytest.raises(ValueError, match="exactly one"):
            call("attachment_put", note_id=note, **args)
