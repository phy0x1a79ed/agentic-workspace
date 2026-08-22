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

from awm.gateway import server as _server
from awm.gateway.server import _stamp_reflection_caller


@pytest.fixture
def resolves(monkeypatch):
    """Stand in for the ancestry walk so these tests never read live /proc."""
    seen = []

    def fake(pid, **kw):
        seen.append(pid)
        return {910: 900}.get(pid, pid)

    monkeypatch.setattr(_server.mcp_caller, "resolve_caller_pid", fake)
    return seen


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

from awm.gateway.mcp_caller import resolve_caller_pid as _resolve_caller_pid


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


def test_stops_at_an_opencode_repl_pid(tmp_path):
    # OpenCode writes no per-pid record file, so the walk must name the process
    # whose exe is `opencode` — the direct analogue of the claude record stop.
    got = _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path),
        ppid_of=_chain({200: 400, 400: 1}),
        is_opencode=lambda pid: pid == 400)
    assert got == 400


def test_opencode_stop_respects_first_match_too(tmp_path):
    # Same safety property as the claude path: an agent nested inside another
    # resolves to its own REPL, never to the session that spawned it.
    got = _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path),
        ppid_of=_chain({200: 400, 400: 500, 500: 1}),
        is_opencode=lambda pid: pid in (400, 500))
    assert got == 400


def test_a_plain_process_does_not_stop_the_walk(tmp_path):
    # The default `is_opencode` reads /proc; injected here as False so a wrapper
    # (bash) between the proxy and the opencode REPL is walked past.
    got = _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path),
        ppid_of=_chain({200: 300, 300: 1}),
        is_opencode=lambda pid: pid == 300)
    assert got == 300


def test_stops_at_init(tmp_path):
    # pid 1 is never a Claude session; walking into it (or past a vanished
    # process, where ppid_of returns None) ends the walk.
    assert _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path), ppid_of=_chain({200: 1})) == 200
    assert _resolve_caller_pid(
        200, sessions_dir=_sessions(tmp_path), ppid_of=lambda pid: None) == 200


# ---------------------------------------------------------------------------
# `X-Awm-Caller-Pid` — the opt-in door for a caller that is not the proxy
# ---------------------------------------------------------------------------

def test_descendant_header_is_resolved_to_its_session(resolves):
    # A hook's own pid names no session, so it is walked to the nearest ancestor
    # that does. Without this the call refuses with "does not look like a Claude
    # Code session" and the hook lane cannot exist at all.
    args = {"verb": "mode", "args": {}}
    _stamp_reflection_caller("reflection", args, None, "910")
    assert args["args"]["_caller_pid"] == 900
    assert resolves == [910]

    flat = {}
    _stamp_reflection_caller("reflection_mode", flat, None, "910")
    assert flat["_caller_pid"] == 900


def test_session_header_is_never_walked(resolves):
    # The whole reason these are two headers: a walk here would turn a pid whose
    # record has vanished into a climb to whatever ancestor session exists —
    # which for a nested agent is the parent's prompt.
    flat = {}
    _stamp_reflection_caller("reflection_mode", flat, "910", None)
    assert flat["_caller_pid"] == 910
    assert resolves == []


def test_session_header_wins_when_both_are_present(resolves):
    flat = {}
    _stamp_reflection_caller("reflection_mode", flat, "2488", "910")
    assert flat["_caller_pid"] == 2488
    assert resolves == []


def test_unresolvable_descendant_is_stamped_unchanged(resolves):
    # No session anywhere in the chain: the walk hands back the pid it was given
    # and reflection refuses it, exactly as it would have before the header
    # existed. The walk may not invent an identity.
    flat = {}
    _stamp_reflection_caller("reflection_mode", flat, None, "777")
    assert flat["_caller_pid"] == 777


def test_non_numeric_descendant_header_strips_a_model_supplied_pid(resolves):
    flat = {"_caller_pid": 999}
    _stamp_reflection_caller("reflection_mode", flat, None, "not-a-pid")
    assert "_caller_pid" not in flat
    assert resolves == []


def test_descendant_header_does_not_touch_other_domains(resolves):
    flat = {}
    _stamp_reflection_caller("scope_refresh", flat, None, "910")
    assert "_caller_pid" not in flat
    assert resolves == []
