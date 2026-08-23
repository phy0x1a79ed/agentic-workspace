"""What the manifest projects onto the MCP/CLI surface.

Worth a test rather than a comment because the failure is silent and only
visible after a deploy: the gateway folds a projected tool name by splitting on
its **first** underscore. ``hermes`` is a single token, so it survives that
fold intact — but only for as long as every ``tool`` name keeps the prefix.
"""

from __future__ import annotations

import pytest

from awm.hermes.hub_adapter import API_MANIFEST, HANDLERS

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

FUNCTIONS = API_MANIFEST["functions"]


def test_every_function_projects_into_a_single_domain():
    for fn in FUNCTIONS:
        domain, _, verb = fn["tool"].partition("_")
        assert domain == "hermes", (fn["name"], fn["tool"])
        assert verb, fn["tool"]


def test_the_projected_verb_matches_the_internal_name():
    """`_find_service_fn` resolves `<domain>_<verb>` back to the internal name,
    so a divergence routes fine — but it makes `awm hermes <verb>` and the
    handler table disagree, which is the kind of drift nobody notices."""
    for fn in FUNCTIONS:
        assert fn["tool"] == f"hermes_{fn['name']}", fn


def test_every_declared_function_has_a_handler():
    assert {fn["name"] for fn in FUNCTIONS} == set(HANDLERS)


def test_the_lifecycle_verbs_carry_a_timeout():
    """They wait on a uvicorn bind, and `start` on a cold node may sit through
    one recovery build of the SPA. The default 30s client ceiling would abort
    mid-launch and report a failure that did not happen."""
    slow = {"start", "restart"}
    for fn in FUNCTIONS:
        if fn["name"] in slow:
            assert fn.get("timeout", 0) >= 240, fn["name"]
