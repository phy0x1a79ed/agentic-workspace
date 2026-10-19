"""What the manifest projects onto the MCP/CLI surface.

Worth a test rather than a comment because the failure is silent and only
visible after a deploy: the gateway folds a function name by splitting on its
**first** underscore, so a two-word service name becomes a domain and a verb
that both read as nonsense (`claude_science_status` → `awm claude
science-status`). Nothing errors; the surface is simply wrong, and the docs
that quote it are wrong with it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from awm.claude_science.hub_adapter import API_MANIFEST, HANDLERS

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

FUNCTIONS = API_MANIFEST["functions"]


def test_every_function_publishes_a_science_tool_over_its_short_name():
    """The tool is `science`, not the service's `claude-science`: a published
    name built from the service would fold into domain `claude`. `name` stays
    the service-internal handle behind `/svc/claude-science/fn/<name>`."""
    for fn in FUNCTIONS:
        assert fn["tool"] == f"science_{fn['name']}", fn


def test_service_toml_declares_the_tool_and_tier():
    toml = tomllib.loads((Path(__file__).parents[1] / "service.toml").read_text())
    assert toml["tool"] == "science"
    assert toml["tier"] == "on"


def test_only_the_daemon_verbs_are_hidden_from_mcp():
    """Hidden verbs keep answering on the CLI and over HTTP."""
    hidden = {fn["tool"] for fn in FUNCTIONS
              if fn.get("surfaces") == ["cli", "http"]}
    assert hidden == {"science_start", "science_stop", "science_restart",
                      "science_logs", "science_update_check"}
    visible = {fn["tool"] for fn in FUNCTIONS} - hidden
    assert visible == {"science_status", "science_url", "science_signin_url",
                       "science_connector", "science_grants",
                       "science_message", "science_new", "science_read"}


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
