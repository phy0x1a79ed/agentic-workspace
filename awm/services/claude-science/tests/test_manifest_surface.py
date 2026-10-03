"""What the manifest projects onto the MCP/CLI surface.

Worth a test rather than a comment because the failure is silent and only
visible after a deploy: the gateway folds a projected tool name by splitting on
its **first** underscore, so a two-word service name becomes a domain and a
verb that both read as nonsense (`claude_science_status` → `awm claude
science-status`). Nothing errors; the surface is simply wrong, and the docs
that quote it are wrong with it.
"""

from __future__ import annotations

import pytest

from awm.claude_science.hub_adapter import API_MANIFEST, HANDLERS

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

FUNCTIONS = API_MANIFEST["functions"]


def test_every_function_projects_into_a_single_domain():
    """The gateway's fold is `name.partition("_")`. One token before the first
    underscore means one domain — here, `science`."""
    for fn in FUNCTIONS:
        domain, _, verb = fn["tool"].partition("_")
        assert domain == "science", (fn["name"], fn["tool"])
        assert verb, fn["tool"]


def test_the_projected_verb_matches_the_internal_name():
    """`_find_service_fn` resolves `<domain>_<verb>` back to the internal name,
    so a divergence routes fine — but it makes `awm science <verb>` and the
    handler table disagree, which is the kind of drift nobody notices."""
    for fn in FUNCTIONS:
        assert fn["tool"] == f"science_{fn['name']}", fn


def test_every_declared_function_has_a_handler():
    assert {fn["name"] for fn in FUNCTIONS} == set(HANDLERS)


def test_the_destructive_verbs_carry_a_timeout():
    """They shell out to a binary that stops and starts a daemon holding a
    ~6 GB environment; the default 30s client ceiling would abort mid-stop and
    leave the socket and owner lock behind."""
    slow = {"start", "stop", "restart", "update_check"}
    for fn in FUNCTIONS:
        if fn["name"] in slow:
            assert fn.get("timeout", 0) >= 180, fn["name"]
