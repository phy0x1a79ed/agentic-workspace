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


def test_every_declared_function_has_a_handler():
    assert {fn["name"] for fn in FUNCTIONS} == set(HANDLERS)


def test_a_verb_that_needs_an_argument_declares_it_required():
    """Marked required, it reaches the CLI as a required option and the MCP
    schema as a required property. Unmarked, the handler raises instead --
    later, and further from the caller."""
    required = {fn["name"]: {p["name"] for p in fn.get("params", [])
                             if p.get("required")}
                for fn in FUNCTIONS}
    assert required["restore"] == {"snapshot"}
    assert required["note_upsert"] == {"title", "content"}
