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


# ---------------------------------------------------------------------------
# `_resolve_caller_pid` — which ancestor the header names in the first place
# ---------------------------------------------------------------------------

from awm.gateway.mcp_server import _resolve_caller_pid


def _sessions(tmp_path, *pids):
    """A Claude Code sessions dir holding a record for each of `pids`."""
    for pid in pids:
        (tmp_path / f"{pid}.json").write_text("{}")
    return str(tmp_path)


def _chain(parent_of):
    """A fake `/proc` ancestry: child pid -> parent pid."""
    return lambda pid: parent_of.get(pid)


def test_direct_child_resolves_to_the_parent(tmp_path):
    # The ordinary case: `.mcp.json` runs the interpreter directly, so the REPL
    # is our parent and the walk stops before it starts.
    got = _resolve_caller_pid(
        100, sessions_dir=_sessions(tmp_path, 100), ppid_of=_chain({100: 1}))
    assert got == 100


def test_walks_past_a_wrapper_process(tmp_path):
    # The regression: `mamba run -n awm awm-mcp` puts pid 200 between the proxy
    # and the REPL at 300. Naming 200 is what made reflection refuse.
    got = _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path, 300), ppid_of=_chain({200: 300, 300: 1}))
    assert got == 300


def test_no_session_in_the_chain_returns_the_parent_unchanged(tmp_path):
    # A genuine non-Claude caller must refuse exactly as it did before the walk
    # existed — the walk may not invent an identity.
    got = _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path), ppid_of=_chain({200: 300, 300: 1}))
    assert got == 200


def test_stops_at_the_nearest_session_not_an_outer_one(tmp_path):
    # An agent nested inside another agent: 400 is our own REPL, 500 is the
    # session that spawned it. Resolving to 500 would type into a *different*
    # agent's prompt, so first-match is a safety property, not an optimisation.
    got = _resolve_caller_pid(
        200,
        sessions_dir=_sessions(tmp_path, 400, 500),
        ppid_of=_chain({200: 400, 400: 500, 500: 1}),
    )
    assert got == 400


def test_walk_is_bounded(tmp_path):
    # A pathological or cyclic chain must not spin; it falls back to the parent.
    got = _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path, 999), ppid_of=lambda pid: pid + 1,
        max_hops=4)
    assert got == 200


def test_stops_at_init(tmp_path):
    # pid 1 is never a Claude session; walking into it (or past a vanished
    # process, where ppid_of returns None) ends the walk.
    assert _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path), ppid_of=_chain({200: 1})) == 200
    assert _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path), ppid_of=lambda pid: None) == 200
