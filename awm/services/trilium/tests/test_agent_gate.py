"""Which notes an agent on another node may write, and slice.

A door call with a session claim is an agent. Its writes need an effective
`#agentWritable`, and an effective `#agentManaged` refuses even under one.
`note_create` alone also admits a parent inside a live slice. The host and
services are never gated, and the vault host's own sessions never get this far
(`test_operator_only`).
"""

from __future__ import annotations

import asyncio

import pytest

from awm.trilium import etapi, hub_adapter

from .fake_vault import FakeVault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

AGENT = {"_caller_session": {"session_id": "s", "node": "mizar"}}


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    monkeypatch.setattr(hub_adapter.config, "edge_url", lambda: "https://vault.example")
    return fake


@pytest.fixture
def tree(vault):
    """root > zone (#agentWritable, inherited) > {leaf, managed (#agentManaged,
    inherited) > record}, and root > closed > inner, which carry no label."""
    ids = {}
    for name, parent in [("zone", "root"), ("leaf", "zone"),
                         ("managed", "zone"), ("record", "managed"),
                         ("closed", "root"), ("inner", "closed")]:
        ids[name] = call("note_create", title=name, parent=ids.get(parent, parent))["note_id"]
    call("attr_set", note_id=ids["zone"], name="agentWritable", inheritable=True)
    call("attr_set", note_id=ids["managed"], name="agentManaged", inheritable=True)
    return ids


def call(verb: str, **args):
    return asyncio.run(hub_adapter.HANDLERS[verb](args, None))


def as_agent(verb: str, **args):
    return asyncio.run(hub_adapter.HANDLERS[verb]({**args, **AGENT}, "peer:mizar"))


def attempt(verb: str, **args):
    """Run the verb as an agent. Returns True when it ran, the refusal when refused."""
    try:
        as_agent(verb, **args)
    except PermissionError as e:
        return str(e)
    return True


# -- single-note writes ------------------------------------------------------


@pytest.mark.parametrize("verb,extra", [
    ("note_update", {"title": "x"}),
    ("note_delete", {}),
    ("attr_set", {"name": "a", "value": "b"}),
    ("attr_delete", {"name": "a"}),
    ("attachment_put", {"title": "a.txt", "content_b64": "aGk="}),
    ("slice_expose", {}),
])
def test_a_write_needs_a_writable_target(tree, verb, extra):
    assert attempt(verb, note_id=tree["leaf"], **extra) is True
    refused = attempt(verb, note_id=tree["closed"], **extra)
    assert "#agentWritable" in refused and tree["closed"] in refused


def test_reads_are_open_to_an_agent(tree):
    assert as_agent("note_get", note_id=tree["closed"])["title"] == "closed"


def test_a_managed_note_is_refused_under_a_writable_subtree(tree):
    for target in ("managed", "record"):
        assert "#agentManaged" in attempt("note_update", note_id=tree[target], title="x")


def test_a_refused_write_does_nothing(vault, tree):
    with pytest.raises(PermissionError):
        as_agent("note_delete", note_id=tree["closed"])
    assert tree["closed"] in vault.notes


@pytest.mark.parametrize("args,as_", [({}, None), ({}, "peer:mizar")])
def test_the_host_and_a_peer_service_are_never_gated(tree, args, as_):
    asyncio.run(hub_adapter.HANDLERS["note_update"](
        {"note_id": tree["closed"], "title": "x", **args}, as_))


# -- two-note writes ---------------------------------------------------------


@pytest.mark.parametrize("verb", ["note_clone", "note_move"])
def test_clone_and_move_need_both_ends(tree, verb):
    zone2 = call("note_create", title="zone2", parent=tree["zone"])["note_id"]
    assert attempt(verb, note_id=tree["leaf"], parent=zone2) is True
    for note_id, parent in [(tree["closed"], tree["zone"]),
                            (tree["leaf"], tree["closed"])]:
        assert "#agentWritable" in attempt(verb, note_id=note_id, parent=parent)


def test_place_needs_the_note_and_every_new_parent(tree):
    assert attempt("note_place", note_id=tree["leaf"], parents=[tree["zone"]]) is True
    assert attempt("note_place", note_id=tree["leaf"],
                   parents=[tree["zone"], tree["closed"]]) is not True
    assert attempt("note_place", note_id=tree["closed"],
                   parents=[tree["zone"]]) is not True


# -- creating inside a live slice --------------------------------------------


def test_create_checks_the_parent(tree):
    assert attempt("note_create", title="n", parent=tree["leaf"]) is True
    assert "#agentWritable" in attempt("note_create", title="n", parent=tree["closed"])


@pytest.mark.parametrize("parent", ["closed", "inner"])
def test_an_agent_creates_under_a_live_slice(vault, tree, parent):
    """Under the slice root or below it. The child is the agent's to keep
    editing. The slice's own notes are not."""
    call("slice_expose", note_id=tree["closed"])
    made = as_agent("note_create", title="n", parent=tree[parent])["note_id"]
    assert vault.labels(made).get("agentWritable") == ""
    assert attempt("note_update", note_id=made, title="mine") is True
    assert "#agentWritable" in attempt("note_update", note_id=tree[parent], title="x")


def test_a_revoked_slice_admits_no_create(tree):
    token = call("slice_expose", note_id=tree["closed"])["token"]
    call("slice_revoke", token=token)
    assert "#agentWritable" in attempt("note_create", title="n", parent=tree["inner"])


def test_a_live_slice_does_not_open_a_managed_note(tree):
    call("slice_expose", note_id=tree["managed"])
    assert "#agentManaged" in attempt("note_create", title="n", parent=tree["record"])


# -- slices ------------------------------------------------------------------


def test_an_agent_lists_and_revokes_only_slices_it_may_write(tree):
    mine = as_agent("slice_expose", note_id=tree["leaf"])["token"]
    tonys = call("slice_expose", note_id=tree["closed"])["token"]
    assert [s["token"] for s in as_agent("slice_list")["slices"]] == [mine]
    assert {s["token"] for s in call("slice_list")["slices"]} == {mine, tonys}
    assert "#agentWritable" in attempt("slice_revoke", token=tonys)
    assert attempt("slice_revoke", token=mine) is True
