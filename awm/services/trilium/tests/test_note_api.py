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


def test_labels_that_are_not_an_object_are_refused(vault):
    with pytest.raises(ValueError, match="object"):
        call("note_create", title="Paper", labels="[1, 2]")


# -- reading -----------------------------------------------------------------


# -- changing ----------------------------------------------------------------


def test_note_update_touches_only_what_it_is_given(vault):
    made = call("note_create", title="Paper", content="<p>x</p>")["note_id"]
    call("note_update", note_id=made, title="Better paper")
    assert vault.notes[made]["title"] == "Better paper"
    assert vault.notes[made]["content"] == "<p>x</p>"


def test_a_second_identical_update_reports_no_change_at_all(vault):
    """What a mirror on a timer asks. Every field is offered on every pass, so
    an update that reports a field as changed merely because it was given makes
    the mirror rewrite the whole library each tick and puts a revision on every
    note in it."""
    made = call("note_create", title="Paper", content="<p>x</p>",
                labels='{"year": "2019"}')["note_id"]
    out = call("note_update", note_id=made, title="Paper", content="<p>x</p>",
               labels='{"year": "2019"}')["changed"]
    assert out == {"content": False}


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


def test_deleting_root_is_refused(vault):
    with pytest.raises(ValueError, match="root is the vault"):
        call("note_delete", note_id="root")


# -- attachments -------------------------------------------------------------


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


def test_a_picture_is_attached_as_an_image(vault):
    """Trilium serves `api/attachments/<id>/image/` only for role "image"."""
    note = call("note_create", title="Sticker")["note_id"]
    pic = call("attachment_put", note_id=note, title="s.webp", content_b64="UklGRg==")
    doc = call("attachment_put", note_id=note, title="p.pdf", content_b64="JVBERg==")
    assert vault.attachments[pic["attachment_id"]]["role"] == "image"
    assert vault.attachments[doc["attachment_id"]]["role"] == "file"


def test_a_change_that_keeps_the_length_is_uploaded(vault):
    """The zotero bundle is one JSON document whose version numbers move
    without changing its length. A length check alone keeps the old bundle."""
    note = call("note_create", title="Library")["note_id"]
    first = call("attachment_put", note_id=note, title="library.json",
                 mime="application/json", content_b64="eyJ2IjogMX0=")  # {"v": 1}
    again = call("attachment_put", note_id=note, title="library.json",
                 mime="application/json", content_b64="eyJ2IjogMn0=")  # {"v": 2}
    assert again["attachment_id"] == first["attachment_id"]
    assert again["changed"] is True
    got = call("attachment_get", attachment_id=first["attachment_id"])
    assert got["content_b64"] == "eyJ2IjogMn0="


def test_a_text_attachment_with_a_multibyte_character_is_not_re_uploaded(
        vault, tmp_path):
    """The defect this fixes, and the reason it hid for so long.

    Trilium decides by mime that an HTML snapshot is text, decodes it, and
    stores it in a SQLite TEXT column. The length it reports back is a count of
    characters. Comparing that to a count of bytes marks every snapshot holding
    one accented letter as changed, on every pass, for ever — twelve files and
    about thirteen megabytes each time in the live vault, with every call
    succeeding and nothing saying why.
    """
    src = tmp_path / "page.html"
    src.write_bytes("<p>Bj\u00f6rk and Ma\u00f1ana</p>".encode("utf-8"))
    assert len(src.read_bytes()) != len(src.read_text("utf-8"))

    note = call("note_create", title="Paper")["note_id"]
    call("attachment_put", note_id=note, path=str(src))
    vault.calls.clear()
    again = call("attachment_put", note_id=note, path=str(src))
    assert again["changed"] is False
    assert not [c for c in vault.calls if c[0] in ("POST", "PUT")]


# -- archived notes ----------------------------------------------------------
