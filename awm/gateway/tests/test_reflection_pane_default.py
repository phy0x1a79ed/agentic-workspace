"""Tests for `_default_reflection_pane` — the /invoke header-to-arg default.

An agent should never need to discover or pass its own tmux pane: `awm-mcp`
forwards it as `X-Awm-Tmux-Pane`, and this is the one place that fills it into
a reflection call's `pane` before the call reaches the catalog. Scoped to the
`reflection` domain only, and never overwrites an explicit `pane`.
"""

import pytest
pytestmark = [pytest.mark.smoke]

from awm.gateway.server import _default_reflection_pane


def test_fills_pane_for_domain_call_when_absent():
    args = {"verb": "compact", "args": {}}
    _default_reflection_pane("reflection", args, "%32")
    assert args["args"]["pane"] == "%32"


def test_fills_pane_for_flat_call_when_absent():
    args = {}
    _default_reflection_pane("reflection_compact", args, "%32")
    assert args["pane"] == "%32"

    args2 = {}
    _default_reflection_pane("reflection_send", args2, "%7")
    assert args2["pane"] == "%7"


def test_never_overwrites_explicit_pane():
    args = {"verb": "compact", "args": {"pane": "%9"}}
    _default_reflection_pane("reflection", args, "%32")
    assert args["args"]["pane"] == "%9"

    flat = {"pane": "%9"}
    _default_reflection_pane("reflection_compact", flat, "%32")
    assert flat["pane"] == "%9"


def test_no_header_leaves_args_untouched():
    args = {"verb": "compact", "args": {}}
    _default_reflection_pane("reflection", args, None)
    assert "pane" not in args["args"]


def test_other_domains_untouched():
    args = {"verb": "post", "args": {}}
    _default_reflection_pane("notes", args, "%32")
    assert "pane" not in args["args"]

    flat = {}
    _default_reflection_pane("scope_refresh", flat, "%32")
    assert "pane" not in flat
