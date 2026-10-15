"""Writing a note into the shared vault, twice, without writing two notes.

The chain script runs on every deploy and every demo re-seed, so `note_upsert`
is called far more often than the note changes. What these tests hold down is
the two ways that goes wrong on a vault nobody owns: a second run that adds a
second note, and a title match loose enough to overwrite somebody else's.
"""

from __future__ import annotations

import httpx
import pytest

from awm.trilium import etapi

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class FakeVault:
    """Just enough ETAPI to hold a note tree in a dict."""

    def __init__(self, notes: dict[str, dict] | None = None) -> None:
        #: note id -> {"title", "content", "children"}
        self.notes = notes or {"root": {"title": "root", "content": "",
                                        "children": []}}
        self.calls: list[tuple[str, str]] = []
        self.next_id = 0

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        path = url.split("/etapi/", 1)[1]
        self.calls.append((method, path))
        request = httpx.Request(method, url)

        if method == "POST" and path == "create-note":
            body = kw["json"]
            self.next_id += 1
            nid = f"note{self.next_id}"
            self.notes[nid] = {"title": body["title"],
                               "content": body["content"], "children": []}
            self.notes[body["parentNoteId"]]["children"].append(nid)
            return httpx.Response(
                201, json={"note": {"noteId": nid, "title": body["title"]},
                           "branch": {}}, request=request)

        head, _, tail = path.partition("/")
        if head != "notes":
            raise AssertionError(f"unexpected {method} {path}")
        note_id, _, sub = tail.partition("/")
        note = self.notes[note_id]
        if sub == "content":
            if method == "GET":
                return httpx.Response(200, text=note["content"],
                                      request=request)
            note["content"] = kw["content"].decode("utf-8")
            return httpx.Response(204, request=request)
        return httpx.Response(200, request=request, json={
            "noteId": note_id, "title": note["title"], "type": "text",
            "childNoteIds": list(note["children"])})


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    return fake


IMG = '<p><img src="/penpot-view/f/p/b"></p>'


def test_the_first_run_creates_the_note(vault):
    out = etapi.client().upsert_note(parent_note_id="root", title="Demo",
                                     content=IMG)
    assert out == {"note_id": "note1", "created": True, "changed": True}
    assert vault.notes["note1"]["content"] == IMG


def test_a_second_run_writes_nothing(vault):
    api = etapi.client()
    first = api.upsert_note(parent_note_id="root", title="Demo", content=IMG)
    vault.calls.clear()
    second = api.upsert_note(parent_note_id="root", title="Demo", content=IMG)
    assert second == {"note_id": first["note_id"], "created": False,
                      "changed": False}
    assert len(vault.notes["root"]["children"]) == 1
    assert not [c for c in vault.calls if c[0] in ("PUT", "POST")]


def test_changed_content_replaces_the_body_in_place(vault):
    api = etapi.client()
    first = api.upsert_note(parent_note_id="root", title="Demo", content=IMG)
    again = api.upsert_note(parent_note_id="root", title="Demo",
                            content=IMG.replace("/b", "/b2"))
    assert again == {"note_id": first["note_id"], "created": False,
                     "changed": True}
    assert len(vault.notes["root"]["children"]) == 1
    assert vault.notes[first["note_id"]]["content"].endswith('/b2"></p>')


def test_a_title_that_is_a_prefix_of_another_is_not_that_note(vault):
    """The reason this matches exactly rather than searching. Trilium's search
    would answer both, and the loser is somebody's writing."""
    api = etapi.client()
    other = api.upsert_note(parent_note_id="root", title="Demo notes",
                            content="<p>mine</p>")
    api.upsert_note(parent_note_id="root", title="Demo", content=IMG)
    assert vault.notes[other["note_id"]]["content"] == "<p>mine</p>"
    assert len(vault.notes["root"]["children"]) == 2


def test_two_notes_of_the_same_title_are_refused(vault):
    api = etapi.client()
    api.upsert_note(parent_note_id="root", title="Demo", content=IMG)
    # A person made a second one by hand. Which body is the stale one is not
    # this verb's call to make.
    api.create_note(parent_note_id="root", title="Demo", type="text",
                    content="<p>theirs</p>")
    with pytest.raises(etapi.EtapiError, match="refusing to guess"):
        api.upsert_note(parent_note_id="root", title="Demo", content=IMG)
    assert vault.notes["note2"]["content"] == "<p>theirs</p>"


def test_a_note_under_a_different_parent_is_a_different_note(vault):
    api = etapi.client()
    folder = api.upsert_note(parent_note_id="root", title="Figures",
                             content="<p></p>")
    under_root = api.upsert_note(parent_note_id="root", title="Demo",
                                 content=IMG)
    under_folder = api.upsert_note(parent_note_id=folder["note_id"],
                                   title="Demo", content=IMG)
    assert under_root["note_id"] != under_folder["note_id"]
