"""The board writer on the operations home reaches the vault across its door.

Its calls arrive stamped ``peer:<home>``. The gate admits them for the note and
attribute verbs, and only under the roots that this host names: the caller's
word never sets the subtree (fleet.md R87 item 3, queue.md R66).
"""

from __future__ import annotations

import asyncio

import pytest

from awm.trilium import etapi, hub_adapter

from .fake_vault import FakeVault

pytestmark = [pytest.mark.unit]

WRITER = "peer:alcor"
BOARD = "awmSwarmBoard"


@pytest.fixture
def vault(monkeypatch):
    monkeypatch.delenv(hub_adapter.WRITER_ROOTS_ENV, raising=False)
    monkeypatch.delenv("AWM_OPERATIONS_HOME", raising=False)
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    asyncio.run(hub_adapter.HANDLERS["note_create"](
        {"title": "outside", "note_id": "outsideNote"}, None))
    return fake


def call(verb: str, as_: str | None = WRITER, **args):
    return asyncio.run(hub_adapter.HANDLERS[verb](args, as_))


def test_the_writer_creates_its_root_and_cards_under_fixed_ids(vault):
    call("note_create", parent="root", note_id=BOARD, title="Swarm", type="render")
    out = call("note_create", parent=BOARD, note_id="swc61", title="card",
               labels={"swarmCard": ""})
    assert out["note_id"] == "swc61"
    call("note_create", parent="swc61", note_id="swr61", title="record")
    assert vault.notes["swr61"]["parents"] == ["swc61"]
    call("note_update", note_id="swr61", content="{}")
    call("attr_set", note_id="swc61", name="section", value="working")
    call("attr_delete", note_id="swc61", name="section")
    assert call("note_get", note_id="swr61")["noteId"] == "swr61"


@pytest.mark.parametrize("verb,args", [
    ("note_create", {"parent": "outsideNote", "title": "x"}),
    ("note_create", {"parent": "root", "note_id": "swcEvil", "title": "x"}),
    ("note_update", {"note_id": "outsideNote", "content": "gone"}),
    ("note_get", {"note_id": "outsideNote"}),
    ("note_delete", {"note_id": "outsideNote"}),
    ("attr_set", {"note_id": "outsideNote", "name": "x", "value": "y"}),
    ("attr_delete", {"note_id": "outsideNote", "name": "x"}),
    ("note_children", {"note_id": "root"}),
    ("note_search", {"query": "#x", "ancestor": "root"}),
])
def test_a_write_outside_the_subtree_is_refused(vault, verb, args):
    call("note_create", parent="root", note_id=BOARD, title="Swarm")
    with pytest.raises(PermissionError, match="board writer"):
        call(verb, **args)
    assert vault.notes["outsideNote"]["content"] == ""


def test_the_subtree_is_this_hosts_setting(vault, monkeypatch):
    monkeypatch.setenv(hub_adapter.WRITER_ROOTS_ENV, "outsideNote")
    call("note_update", note_id="outsideNote", content="mine now")
    with pytest.raises(PermissionError):
        call("note_create", parent="root", title="x")


def test_root_is_never_a_writer_root(vault, monkeypatch):
    monkeypatch.setenv(hub_adapter.WRITER_ROOTS_ENV, "root")
    with pytest.raises(PermissionError, match="no writer subtree"):
        call("note_get", note_id="outsideNote")


def test_search_without_an_ancestor_answers_only_inside(vault):
    call("note_create", parent="root", note_id=BOARD, title="Swarm")
    call("note_create", parent=BOARD, note_id="inNote", title="in", labels={"swarmOps": ""})
    call("attr_set", as_=None, note_id="outsideNote", name="swarmOps", value="")
    hits = call("note_search", query="#swarmOps")["results"]
    assert [h["noteId"] for h in hits] == ["inNote"]
    assert len(call("note_search", as_=None, query="#swarmOps")["results"]) == 2


def test_a_missing_note_reads_as_not_found(vault):
    call("note_create", parent="root", note_id=BOARD, title="Swarm")
    with pytest.raises(etapi.EtapiError, match="404"):
        call("note_get", note_id="swcNone")


@pytest.mark.parametrize("as_", ["peer:mizar", "user:tony", "peer"])
def test_another_caller_is_still_refused(vault, as_):
    call("note_create", parent="root", note_id=BOARD, title="Swarm")
    with pytest.raises(PermissionError, match="operator verb"):
        call("note_update", as_=as_, note_id=BOARD, content="x")


def test_the_writer_gets_no_other_verb(vault):
    with pytest.raises(PermissionError, match="operator verb"):
        call("note_move", note_id="outsideNote", parent="root")
