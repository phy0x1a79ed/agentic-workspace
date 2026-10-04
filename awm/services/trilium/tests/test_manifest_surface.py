"""What the manifest projects onto the MCP/CLI surface.

Worth a test rather than a comment because the failure is silent and only
visible after a deploy: the gateway folds a projected tool name by splitting on
its **first** underscore, so a name that carries an extra underscore becomes a
domain and a verb that both read as nonsense. Nothing errors; the surface is
simply wrong, and the docs that quote it are wrong with it.
"""

from __future__ import annotations

import pytest

from awm.trilium.hub_adapter import API_MANIFEST, HANDLERS

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

FUNCTIONS = API_MANIFEST["functions"]


def test_every_function_projects_into_a_single_domain():
    for fn in FUNCTIONS:
        domain, _, verb = fn["tool"].partition("_")
        assert domain == "trilium", (fn["name"], fn["tool"])
        assert verb, fn["tool"]


def test_the_projected_verb_matches_the_internal_name():
    """A divergence routes fine but makes `awm trilium <verb>` and the handler
    table disagree, which is the kind of drift nobody notices."""
    for fn in FUNCTIONS:
        assert fn["tool"] == f"trilium_{fn['name']}", fn


def test_every_declared_function_has_a_handler():
    assert {fn["name"] for fn in FUNCTIONS} == set(HANDLERS)


def test_every_declared_parameter_reaches_the_catalog():
    """The manifest key is `params`, and nothing complains about any other.

    `catalog._fn_to_tool` reads `fn.get("params", [])`, so a function that
    spells it `parameters` projects a tool with an empty inputSchema: the verb
    still appears, still dispatches, and every flag it declares is silently
    gone. That is exactly what shipped here -- `awm trilium restore` offered
    no --snapshot at all -- and nothing errored, which is why it is a test and
    not a comment.
    """
    from awm.gateway.catalog import _fn_to_tool

    class _Rec:
        name = "trilium"

    for fn in FUNCTIONS:
        declared = {p["name"] for p in fn.get("params", [])}
        projected = set(_fn_to_tool(_Rec(), fn).inputSchema["properties"])
        assert declared == projected, fn["name"]
        assert not set(fn) & {"parameters"}, fn["name"]


def test_a_verb_that_needs_an_argument_declares_it_required():
    """Marked required, it reaches the CLI as a required option and the MCP
    schema as a required property. Unmarked, the handler raises instead --
    later, and further from the caller."""
    required = {fn["name"]: {p["name"] for p in fn.get("params", [])
                             if p.get("required")}
                for fn in FUNCTIONS}
    assert required["restore"] == {"snapshot"}
    assert required["note_upsert"] == {"title", "content"}


def test_the_lifecycle_verbs_outlast_the_client_default():
    """They spawn or signal a node process and wait for a port to bind or free.
    Trilium runs a schema migration on first start, so the default 30s client
    ceiling would abort mid-start and leave the caller holding a timeout
    instead of a result."""
    for fn in FUNCTIONS:
        if fn["name"] in {"start", "stop", "restart"}:
            assert fn.get("timeout", 0) >= 120, fn["name"]
