"""Which notes a session may write.

The gateway stamps `_caller_pid` on a session's call and on nothing else, so
that key is the whole discriminator: a call without it is the host or a
service, and the labels are never read.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from awm.trilium import etapi, hub_adapter

from .fake_vault import FakeVault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

SESSION = {"_caller_pid": 4242}


@pytest.fixture
def vault(monkeypatch):
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    return fake


@pytest.fixture
def tree(vault):
    """root > zone (#agentWritable, inherited) > {leaf, managed (#agentManaged,
    inherited) > record}, and root > closed, which carries no label."""
    ids = {}
    for name, parent in [("zone", "root"), ("leaf", "zone"),
                         ("managed", "zone"), ("record", "managed"),
                         ("closed", "root")]:
        ids[name] = call("note_create", title=name, parent=ids.get(parent, parent))["note_id"]
    call("attr_set", note_id=ids["zone"], name="agentWritable", inheritable=True)
    call("attr_set", note_id=ids["managed"], name="agentManaged", inheritable=True)
    return ids


def call(verb: str, **args):
    return asyncio.run(hub_adapter.HANDLERS[verb](args, None))


def as_session(verb: str, **args):
    return call(verb, **args, **SESSION)


@pytest.fixture(params=["enforce", "log"])
def mode(request, monkeypatch):
    monkeypatch.setenv("AWM_TRILIUM_AGENT_GATE", request.param)
    return request.param


def attempt(mode, verb, **args):
    """Run the verb as a session. Returns True when it ran, False when refused."""
    try:
        as_session(verb, **args)
    except PermissionError as e:
        assert mode == "enforce"
        return str(e)
    return True


# -- single-note writes ------------------------------------------------------


@pytest.mark.parametrize("verb,extra", [
    ("note_update", {"title": "x"}),
    ("note_delete", {}),
    ("attr_set", {"name": "a", "value": "b"}),
    ("attr_delete", {"name": "a"}),
    ("attachment_put", {"title": "a.txt", "content_b64": "aGk="}),
])
def test_a_write_needs_a_writable_target(mode, tree, verb, extra):
    ok = attempt(mode, verb, note_id=tree["leaf"], **extra)
    assert ok is True
    refused = attempt(mode, verb, note_id=tree["closed"], **extra)
    assert (refused is True) == (mode == "log")
    if mode == "enforce":
        assert "#agentWritable" in refused and tree["closed"] in refused


def test_create_checks_the_parent(mode, tree):
    assert attempt(mode, "note_create", title="n", parent=tree["leaf"]) is True
    refused = attempt(mode, "note_create", title="n", parent=tree["closed"])
    assert (refused is True) == (mode == "log")


def test_upsert_checks_the_parent(mode, tree):
    assert attempt(mode, "note_upsert", title="n", content="c",
                   parent=tree["leaf"]) is True
    refused = attempt(mode, "note_upsert", title="n", content="c",
                      parent=tree["closed"])
    assert (refused is True) == (mode == "log")


def test_a_managed_note_is_refused_under_a_writable_subtree(mode, tree):
    for target in ("managed", "record"):
        refused = attempt(mode, "note_update", note_id=tree[target], title="x")
        assert (refused is True) == (mode == "log")
        if mode == "enforce":
            assert "#agentManaged" in refused


def test_nothing_may_be_created_under_a_managed_note(mode, tree):
    refused = attempt(mode, "note_create", title="n", parent=tree["record"])
    assert (refused is True) == (mode == "log")


# -- two-note writes ---------------------------------------------------------


@pytest.mark.parametrize("verb,key", [("note_clone", "parent"),
                                      ("note_move", "parent")])
def test_clone_and_move_need_both_ends(mode, tree, verb, key):
    zone2 = call("note_create", title="zone2", parent=tree["zone"])["note_id"]
    assert attempt(mode, verb, note_id=tree["leaf"], **{key: zone2}) is True
    for note_id, parent in [(tree["closed"], tree["zone"]),
                            (tree["leaf"], tree["closed"])]:
        out = attempt(mode, verb, note_id=note_id, parent=parent)
        assert (out is True) == (mode == "log")
        if mode == "enforce":
            assert "#agentWritable" in out


def test_place_needs_the_note_and_every_new_parent(mode, tree):
    assert attempt(mode, "note_place", note_id=tree["leaf"],
                   parents=[tree["zone"]]) is True
    out = attempt(mode, "note_place", note_id=tree["leaf"],
                  parents=[tree["zone"], tree["closed"]])
    assert (out is True) == (mode == "log")
    out = attempt(mode, "note_place", note_id=tree["closed"],
                  parents=[tree["zone"]])
    assert (out is True) == (mode == "log")


# -- who and when ------------------------------------------------------------


def test_a_refused_write_does_nothing_in_enforce_mode(monkeypatch, vault, tree):
    monkeypatch.setenv("AWM_TRILIUM_AGENT_GATE", "enforce")
    with pytest.raises(PermissionError):
        as_session("note_delete", note_id=tree["closed"])
    assert tree["closed"] in vault.notes


def test_log_mode_records_the_refusal_and_writes(monkeypatch, tree, caplog):
    monkeypatch.delenv("AWM_TRILIUM_AGENT_GATE", raising=False)
    with caplog.at_level(logging.WARNING, logger="awm.trilium.hub_adapter"):
        as_session("note_update", note_id=tree["closed"], title="changed")
    assert call("note_get", note_id=tree["closed"])["title"] == "changed"
    assert "#agentWritable" in caplog.text


def test_off_mode_does_not_read_labels(monkeypatch, vault, tree):
    monkeypatch.setenv("AWM_TRILIUM_AGENT_GATE", "off")
    vault.calls.clear()
    as_session("note_update", note_id=tree["closed"], title="changed")
    assert vault.calls.count(("GET", f"notes/{tree['closed']}")) == 1


@pytest.mark.parametrize("verb,extra", [
    ("note_update", {"title": "x"}), ("note_delete", {}),
    ("attr_set", {"name": "a"}),
])
def test_a_service_caller_is_never_gated(monkeypatch, tree, verb, extra):
    monkeypatch.setenv("AWM_TRILIUM_AGENT_GATE", "enforce")
    call(verb, note_id=tree["closed"], **extra)


def test_reads_are_never_gated(monkeypatch, tree):
    monkeypatch.setenv("AWM_TRILIUM_AGENT_GATE", "enforce")
    for verb in ("note_get", "note_children", "attrs_get"):
        as_session(verb, note_id=tree["closed"])
    as_session("note_search", query="#agentWritable")
