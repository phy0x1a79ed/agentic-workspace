"""Tests for `_stamp_reflection_caller` — the /invoke header-to-arg identity stamp.

An agent should never need to discover or pass anything about itself: `awm-mcp`
runs as a stdio child of the calling session and forwards its parent pid as
`X-Awm-Session-Pid`, and this is the one place that attaches it to a reflection
call before the call reaches the catalog.

The stamp is authoritative, not a default. Reflection injects into the caller's
own prompt, so a `_caller_pid` the model could set would be a way to type into
another agent's session — hence it is always overwritten from the header, and
removed outright when there is no header.
"""

import pytest
pytestmark = [pytest.mark.smoke]

from awm.gateway.server import _stamp_reflection_caller


def test_stamps_pid_for_domain_call():
    args = {"verb": "compact", "args": {}}
    _stamp_reflection_caller("reflection", args, "2488")
    assert args["args"]["_caller_pid"] == 2488


def test_stamps_pid_for_flat_call():
    args = {}
    _stamp_reflection_caller("reflection_compact", args, "2488")
    assert args["_caller_pid"] == 2488

    args2 = {}
    _stamp_reflection_caller("reflection_send", args2, "77")
    assert args2["_caller_pid"] == 77


def test_creates_the_args_bag_when_the_domain_call_omits_it():
    # A no-argument `reflection(verb="compact")` arrives with no inner bag at
    # all; identity still has to land somewhere.
    args = {"verb": "compact"}
    _stamp_reflection_caller("reflection", args, "2488")
    assert args["args"]["_caller_pid"] == 2488


def test_model_supplied_caller_pid_is_overwritten():
    args = {"verb": "compact", "args": {"_caller_pid": 999}}
    _stamp_reflection_caller("reflection", args, "2488")
    assert args["args"]["_caller_pid"] == 2488

    flat = {"_caller_pid": 999}
    _stamp_reflection_caller("reflection_compact", flat, "2488")
    assert flat["_caller_pid"] == 2488


def test_model_supplied_caller_pid_is_stripped_without_a_header():
    # No header means no known caller. Leaving the model's value in place would
    # let an agent name a target; it must be removed, not honoured.
    args = {"verb": "compact", "args": {"_caller_pid": 999}}
    _stamp_reflection_caller("reflection", args, None)
    assert "_caller_pid" not in args["args"]

    flat = {"_caller_pid": 999}
    _stamp_reflection_caller("reflection_compact", flat, None)
    assert "_caller_pid" not in flat


def test_non_numeric_header_is_ignored():
    args = {"verb": "compact", "args": {"_caller_pid": 999}}
    _stamp_reflection_caller("reflection", args, "not-a-pid")
    assert "_caller_pid" not in args["args"]


def test_covers_verbs_added_after_this_was_written():
    # The flat surface is `<domain>_<verb>` and service names cannot contain an
    # underscore, so every reflection verb is covered by construction — a
    # hand-maintained name list would leave new verbs unidentified, which reads
    # as "no caller" and refuses every call to them.
    for verb in ("mode", "whoami", "some_future_verb"):
        flat = {}
        _stamp_reflection_caller(f"reflection_{verb}", flat, "2488")
        assert flat["_caller_pid"] == 2488, verb


def test_other_domains_untouched():
    args = {"verb": "post", "args": {"_caller_pid": 5}}
    _stamp_reflection_caller("notes", args, "2488")
    assert args["args"]["_caller_pid"] == 5

    flat = {}
    _stamp_reflection_caller("scope_refresh", flat, "2488")
    assert "_caller_pid" not in flat
