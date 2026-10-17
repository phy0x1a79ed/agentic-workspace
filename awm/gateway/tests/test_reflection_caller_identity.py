"""Tests for `_stamp_caller` — the /invoke header-to-arg identity stamp.

An agent should never need to discover or pass anything about itself: `awm-mcp`
runs as a stdio child of the calling session and forwards its parent pid as
`X-Awm-Session-Pid`, and this is the one place that attaches it to a call that
acts on a session (every reflection verb, `start_other` and `stop_other`
included, and every operations verb) before the call reaches the catalog.

The stamp is authoritative, not a default. Reflection injects into the caller's
own prompt, and `start_other` records the caller as the new session's parent, so a
`_caller_pid` the model could set would name another agent — hence it is always
overwritten from the header, and removed outright when there is no header.
"""

import json

import pytest
pytestmark = [pytest.mark.smoke]

from awm.gateway import server as _server
from awm.gateway.server import _stamp_caller


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
    _stamp_caller("reflection", args, "2488")
    assert args["args"]["_caller_pid"] == 2488


def test_stamps_pid_for_flat_call():
    args = {}
    _stamp_caller("reflection_compact", args, "2488")
    assert args["_caller_pid"] == 2488

    args2 = {}
    _stamp_caller("reflection_message_self", args2, "77")
    assert args2["_caller_pid"] == 77


def test_creates_the_args_bag_when_the_domain_call_omits_it():
    # A no-argument `reflection(verb="compact")` arrives with no inner bag at
    # all; identity still has to land somewhere.
    args = {"verb": "compact"}
    _stamp_caller("reflection", args, "2488")
    assert args["args"]["_caller_pid"] == 2488


def test_model_supplied_caller_pid_is_overwritten():
    args = {"verb": "compact", "args": {"_caller_pid": 999}}
    _stamp_caller("reflection", args, "2488")
    assert args["args"]["_caller_pid"] == 2488

    flat = {"_caller_pid": 999}
    _stamp_caller("reflection_compact", flat, "2488")
    assert flat["_caller_pid"] == 2488


def test_model_supplied_caller_pid_is_stripped_without_a_header():
    # No header means no known caller. Leaving the model's value in place would
    # let an agent name a target; it must be removed, not honoured.
    args = {"verb": "compact", "args": {"_caller_pid": 999}}
    _stamp_caller("reflection", args, None)
    assert "_caller_pid" not in args["args"]

    flat = {"_caller_pid": 999}
    _stamp_caller("reflection_compact", flat, None)
    assert "_caller_pid" not in flat


def test_non_numeric_header_is_ignored():
    args = {"verb": "compact", "args": {"_caller_pid": 999}}
    _stamp_caller("reflection", args, "not-a-pid")
    assert "_caller_pid" not in args["args"]


def test_covers_verbs_added_after_this_was_written():
    # The flat surface is `<domain>_<verb>` and service names cannot contain an
    # underscore, so every reflection verb is covered by construction — a
    # hand-maintained name list would leave new verbs unidentified, which reads
    # as "no caller" and refuses every call to them.
    for verb in ("mode", "whoami", "some_future_verb"):
        flat = {}
        _stamp_caller(f"reflection_{verb}", flat, "2488")
        assert flat["_caller_pid"] == 2488, verb


def test_other_domains_untouched():
    args = {"verb": "post", "args": {"_caller_pid": 5}}
    _stamp_caller("notes", args, "2488")
    assert args["args"]["_caller_pid"] == 5

    flat = {}
    _stamp_caller("journal_fetch", flat, "2488")
    assert "_caller_pid" not in flat


def test_a_journal_post_is_stamped_with_its_session():
    flat = {"_caller_pid": 5}
    _stamp_caller("journal_post", flat, "2488")
    assert flat["_caller_pid"] == 2488
    domain = {"verb": "post", "args": {}}
    _stamp_caller("journal", domain, "2488")
    assert domain["args"]["_caller_pid"] == 2488


# ---------------------------------------------------------------------------
# `start_other` and `stop_other`: cx's verbs, published under reflection
# ---------------------------------------------------------------------------
#
# Losing this stamp silently drops delegate confinement (a delegate's lineage is
# read from the pid) and the stop-ownership check, so each shape is pinned.


@pytest.mark.parametrize("verb", ["start_other", "stop_other"])
def test_start_other_and_stop_other_are_stamped_in_both_shapes(verb):
    nested = {"verb": verb, "args": {}}
    _stamp_caller("reflection", nested, "2488")
    assert nested["args"]["_caller_pid"] == 2488

    flat = {}
    _stamp_caller(f"reflection_{verb}", flat, "2488")
    assert flat["_caller_pid"] == 2488


@pytest.mark.parametrize("verb", ["list_other", "claim_other", "seed_other", "remove_other"])
def test_the_other_cx_verbs_ride_the_reflection_stamp_too(verb):
    flat = {}
    _stamp_caller(f"reflection_{verb}", flat, "2488")
    assert flat["_caller_pid"] == 2488


def test_a_domain_call_without_an_args_bag_to_start_other_gets_one():
    args = {"verb": "start_other"}
    _stamp_caller("reflection", args, "2488")
    assert args["args"]["_caller_pid"] == 2488


@pytest.mark.parametrize("pid_header", ["2488", None, "not-a-pid"])
def test_model_supplied_caller_pid_on_start_other_never_survives(pid_header):
    nested = {"verb": "start_other", "args": {"project": "awm", "_caller_pid": 999}}
    _stamp_caller("reflection", nested, pid_header)
    assert nested["args"].get("_caller_pid") != 999
    assert nested["args"]["project"] == "awm"

    flat = {"_caller_pid": 999}
    _stamp_caller("reflection_stop_other", flat, pid_header)
    assert flat.get("_caller_pid") != 999


def test_start_other_descendant_header_is_walked(resolves):
    flat = {}
    _stamp_caller("reflection_start_other", flat, None, "910")
    assert flat["_caller_pid"] == 900 and resolves == [910]


def test_the_retired_cx_tool_is_no_longer_a_stamped_key():
    flat = {}
    _stamp_caller("cx_start", flat, "2488")
    assert "_caller_pid" not in flat


def test_a_tool_that_shares_a_prefix_with_reflection_is_left_alone():
    flat = {}
    _stamp_caller("reflectionx_start_other", flat, "2488")
    assert "_caller_pid" not in flat


# ---------------------------------------------------------------------------
# an edge-forwarded request carries no trusted pid header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,shape", [
    ("reflection", "nested"), ("reflection_compact", "flat"),
    ("operations", "nested"), ("reflection_start_other", "flat"),
])
def test_pid_headers_are_ignored_when_the_request_carries_x_awm_as(name, shape):
    if shape == "nested":
        args = {"verb": "status" if name == "operations" else "compact",
                "args": {"_caller_pid": 999}}
        bag = lambda: args["args"]  # noqa: E731
    else:
        args = {"_caller_pid": 999}
        bag = lambda: args  # noqa: E731
    _stamp_caller(name, args, "2488", "77", "peer:capella")
    assert "_caller_pid" not in bag()


def test_pid_header_still_stamps_without_x_awm_as():
    args = {}
    _stamp_caller("reflection_start_other", args, "2488", None, None)
    assert args["_caller_pid"] == 2488


# ---------------------------------------------------------------------------
# the /svc/<svc>/fn/<fn> door stamps like /invoke
# ---------------------------------------------------------------------------


def _record(name, **published):
    from awm.gateway.hub.registry import ServiceRecord

    return ServiceRecord(name=name, prefix=f"/svc/{name}", kind="service",
                         api={"functions": [{"name": fn, "tool": tool}
                                            for fn, tool in published.items()]})


class _Registry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


#: What the services publish. The door's path names the internal function, so the
#: stamp has to look the published name up.
_PUBLISHED = [
    _record("cx", start="reflection_start_other", stop="reflection_stop_other",
            list="reflection_list_other"),
    _record("reporting", claim_task="operations_task_claim"),
    _record("reflection", compact="reflection_compact"),
]


class _FakeChannel:
    def __init__(self):
        import asyncio

        self.ready = asyncio.Event()
        self.ready.set()
        self.calls = []

    def function_spec(self, fn):
        return {}

    async def call(self, fn, args, as_=None, timeout=30.0, grants=None):
        self.calls.append((fn, args))
        return {"ok": True}


async def _post_svc(monkeypatch, svc, fn, body, headers=None, as_=None):
    from starlette.datastructures import Headers
    from starlette.requests import Request

    from awm.gateway.hub import proxy

    ch = _FakeChannel()
    monkeypatch.setattr(proxy.rpc, "get_control", lambda sid: ch)
    monkeypatch.setattr(_server.catalog, "get_registry", lambda: _Registry(_PUBLISHED))
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": f"/svc/{svc}/fn/{fn}",
             "headers": [(b"content-type", b"application/json")], "query_string": b""}
    request = Request(scope, receive)
    hdr = Headers(headers=headers or {})
    await proxy.proxy_service_http(request, "sid", as_=as_,
                                   stamp=_server._svc_stamp(svc, hdr, as_))
    return ch.calls[0][1]



@pytest.mark.parametrize("svc,fn", [("cx", "start"), ("cx", "stop"),
                                    ("reflection", "compact")])
async def test_svc_door_strips_a_body_supplied_caller_pid(monkeypatch, svc, fn):
    args = await _post_svc(monkeypatch, svc, fn, {"project": "awm", "_caller_pid": 999})
    assert "_caller_pid" not in args and args["project"] == "awm"


async def test_svc_door_stamps_from_the_header(monkeypatch):
    args = await _post_svc(monkeypatch, "cx", "start", {"_caller_pid": 999},
                           headers={"X-Awm-Session-Pid": "2488"})
    assert args["_caller_pid"] == 2488


async def test_svc_door_ignores_the_header_on_an_edge_request(monkeypatch):
    args = await _post_svc(monkeypatch, "cx", "start", {"_caller_pid": 999},
                           headers={"X-Awm-Session-Pid": "2488"}, as_="peer:capella")
    assert "_caller_pid" not in args


async def test_svc_door_stamps_cx_by_the_name_it_publishes_under(monkeypatch):
    """`/svc/cx/fn/stop` names the internal function; the stamp is keyed by the
    published `reflection_stop_other`."""
    for fn in ("start", "stop", "list"):
        args = await _post_svc(monkeypatch, "cx", fn, {"_caller_pid": 999},
                               headers={"X-Awm-Session-Pid": "2488"})
        assert args["_caller_pid"] == 2488, fn


async def test_svc_door_stamps_reporting_under_operations(monkeypatch):
    args = await _post_svc(monkeypatch, "reporting", "claim_task", {"_caller_pid": 999},
                           headers={"X-Awm-Session-Pid": "2488"})
    assert args["_caller_pid"] == 2488


async def test_svc_door_leaves_other_services_alone(monkeypatch):
    args = await _post_svc(monkeypatch, "notes", "post", {"_caller_pid": 5})
    assert args["_caller_pid"] == 5


async def test_svc_door_with_no_body_still_calls_with_no_args(monkeypatch):
    args = await _post_svc(monkeypatch, "cx", "list", None)
    assert args is None


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
    _stamp_caller("reflection", args, None, "910")
    assert args["args"]["_caller_pid"] == 900
    assert resolves == [910]

    flat = {}
    _stamp_caller("reflection_mode", flat, None, "910")
    assert flat["_caller_pid"] == 900


def test_session_header_is_never_walked(resolves):
    # The whole reason these are two headers: a walk here would turn a pid whose
    # record has vanished into a climb to whatever ancestor session exists —
    # which for a nested agent is the parent's prompt.
    flat = {}
    _stamp_caller("reflection_mode", flat, "910", None)
    assert flat["_caller_pid"] == 910
    assert resolves == []


def test_session_header_wins_when_both_are_present(resolves):
    flat = {}
    _stamp_caller("reflection_mode", flat, "2488", "910")
    assert flat["_caller_pid"] == 2488
    assert resolves == []


def test_unresolvable_descendant_is_stamped_unchanged(resolves):
    # No session anywhere in the chain: the walk hands back the pid it was given
    # and reflection refuses it, exactly as it would have before the header
    # existed. The walk may not invent an identity.
    flat = {}
    _stamp_caller("reflection_mode", flat, None, "777")
    assert flat["_caller_pid"] == 777


def test_non_numeric_descendant_header_strips_a_model_supplied_pid(resolves):
    flat = {"_caller_pid": 999}
    _stamp_caller("reflection_mode", flat, None, "not-a-pid")
    assert "_caller_pid" not in flat
    assert resolves == []


def test_descendant_header_does_not_touch_other_domains(resolves):
    flat = {}
    _stamp_caller("journal_render", flat, None, "910")
    assert "_caller_pid" not in flat
    assert resolves == []


def test_reporting_and_trilium_writes_are_stamped_and_the_body_caller_dropped():
    reporting = {"verb": "task_claim", "args": {"_caller": "x", "_caller_pid": 9}}
    _stamp_caller("operations", reporting, "2488")
    assert reporting["args"] == {"_caller_pid": 2488}

    flat = {"_caller": "x", "_caller_pid": 9}
    _stamp_caller("trilium_note_update", flat, None)
    assert flat == {}


def test_trilium_reads_are_not_stamped():
    args = {"verb": "note_get", "args": {"_caller_pid": 9}}
    _stamp_caller("trilium", args, "2488")
    assert args["args"] == {"_caller_pid": 9}
