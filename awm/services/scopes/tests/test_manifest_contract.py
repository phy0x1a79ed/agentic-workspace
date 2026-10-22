"""The manifest and the handlers have to agree, and nothing checks that at import.

A manifest entry is data and a handler is code, so a param can go on being
advertised after the function behind it stops accepting one — and the failure
surfaces as a ``TypeError`` at call time, on a verb no unit test exercises. That
is how ``gather``/``scatter`` kept a ``data=True`` leg after the layer
implementing it was deleted: the manifest still offered it and the handler still
forwarded it to a signature that no longer had it.

Two different keys are in play and they are easy to confuse. Dispatch looks a
handler up by the entry's **name**; the CLI and MCP surfaces publish the
entry's **tool**, ``scope_<verb>``. They are usually equal, and ``scope_gather``
published as ``scope_git_gather`` is the deliberate exception.
"""

from __future__ import annotations

import inspect

import pytest

from awm.scopes import scopes
from awm.scopes.operations.scopes import SCOPE_HANDLERS, SCOPE_MANIFEST_FUNCTIONS

pytestmark = [pytest.mark.scopes]


def test_every_manifest_function_has_a_handler():
    declared = {f["name"] for f in SCOPE_MANIFEST_FUNCTIONS}
    assert declared <= set(SCOPE_HANDLERS), declared - set(SCOPE_HANDLERS)


def test_no_handler_is_registered_without_a_manifest_entry():
    declared = {f["name"] for f in SCOPE_MANIFEST_FUNCTIONS}
    assert set(SCOPE_HANDLERS) <= declared, set(SCOPE_HANDLERS) - declared


def test_a_tool_name_carries_its_domain_prefix():
    """An unprefixed tool name splits off a single-verb domain of its own.

    ``data_gc`` shipped once as ``awm data gc``, beside ``awm scope data-status``
    — the prefix is the only thing that routes it.
    """
    for f in SCOPE_MANIFEST_FUNCTIONS:
        assert f["tool"].startswith("scope_"), f["tool"]


def test_the_service_publishes_exactly_the_scope_tool_verbs():
    from awm.scopes.hub_adapter import API_MANIFEST
    published = {f["tool"]: f for f in API_MANIFEST["functions"]}
    visible = {n for n, f in published.items() if "surfaces" not in f}
    assert visible == {
        "scope_resolve", "scope_ensure", "scope_create", "scope_search", "scope_complete",
        "scope_delete", "scope_heal", "scope_private", "scope_tag", "scope_repair", "scope_sync",
        "scope_git_gather", "scope_git_scatter",
    }
    hidden = {n for n, f in published.items() if f.get("surfaces") == ["cli", "http"]}
    assert hidden == {"scope_ref_resolve", "scope_reindex", "project_ensure"}


def test_the_identity_rpcs_keep_their_internal_names():
    from awm.scopes.hub_adapter import API_MANIFEST, HANDLERS
    names = {f["name"] for f in API_MANIFEST["functions"]}
    assert {"resolveScope", "resolveRef", "ensureProject", "ensureScope"} <= names
    assert {"resolveScope", "resolveRef", "ensureProject", "ensureScope"} <= set(HANDLERS)


def test_the_removed_verbs_are_gone():
    from awm.scopes.hub_adapter import API_MANIFEST, HANDLERS
    gone = {"project_create", "project_search", "scope_archive_search", "awm_refresh", "scope_refresh"}
    assert not gone & {f["tool"] for f in API_MANIFEST["functions"]}
    assert not gone & set(HANDLERS)


@pytest.mark.parametrize("verb,func", [
    ("scope_gather", "gather_scope"),
    ("scope_scatter", "scatter_scope"),
])
def test_gather_and_scatter_forward_only_what_their_target_accepts(
    monkeypatch, verb, func
):
    """The regression: a handler forwarding a kwarg the function has dropped.

    A peripheral's data pins ride its code branch, so merging the branch already
    moves the data — there is no data leg to offer, and offering a dead one is
    worse than offering nothing, since a caller who passes it gets a TypeError
    rather than a no-op.
    """
    assert "data" not in inspect.signature(getattr(scopes, func)).parameters
    assert not [
        p for f in SCOPE_MANIFEST_FUNCTIONS if f["name"] == verb
        for p in f["params"] if p["name"] == "data"
    ]

    seen: dict = {}

    class _Result:
        def model_dump(self):
            return {"ok": True}

    def _fake(*a, **k):
        seen.update(args=a, kwargs=k)
        return _Result()

    monkeypatch.setattr(scopes, func, _fake)
    out = SCOPE_HANDLERS[verb](
        {"project": "p", "hub": "h", "peripherals": ["a"], "strategy": "merge"}
    )

    assert out == {"ok": True}
    assert "data" not in seen["kwargs"]
