"""The session mode gate on `/invoke` and the `/svc/<svc>/fn/<fn>` door.

A session cx started in a restricted mode may call only the verbs
`awm.config.modes` lists for it. The gateway finds the mode from the pid header
through `awm.claudedaemon.sessionmode`, which reads files on disk. These tests
cover the gate's decision for each kind of policy, both call shapes and both
doors, the fail-closed lookups, and the per-pid cache. The built-in modes are
only `unknown`, so the tests register their own: `fixed` and `fixed2` name
their verbs (`Allow`), and `reader` admits by declared effect (`ByEffect`). The
last group runs the real tri-state lookup against a temporary Claude home.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.hub, pytest.mark.smoke]

from awm.config import modes as policies
from awm.gateway import server

PID = os.getpid()
HEAD = {"X-Awm-Session-Pid": str(PID)}


def _pairs(*items):
    return frozenset(tuple(i.split(".")) for i in items)


def _is_reply(call_args):
    reply_to = call_args.get("reply_to")
    return call_args.get("kind") == "message" and isinstance(reply_to, str) and bool(reply_to)


_FIXED = _pairs(
    "operations.board_list", "operations.board_get", "operations.board_fail",
    "operations.board_messages", "reflection.start_other", "reflection.list_other",
    "reflection.compact", "reflection.whoami", "operations.status", "operations.queue_list",
    "operations.queue_get", "operations.queue_assign", "journal.fetch", "journal.goal_read",
    "scope.search", "scope.resolve", "search.query")
_FIXED2 = _pairs(
    "reflection.start_other", "reflection.list_other", "reflection.stop_other",
    "operations.board_list", "operations.board_get", "operations.status",
    "operations.queue_list", "operations.queue_get", "reflection.compact", "journal.fetch")
_CARDS = _pairs("operations.queue_list", "operations.queue_get", "operations.queue_assign",
                "operations.status")


@pytest.fixture(autouse=True)
def test_policies(monkeypatch):
    monkeypatch.setitem(policies.MODES, "fixed", policies.Allow(_FIXED))
    monkeypatch.setitem(policies.MODES, "fixed2", policies.Allow(_FIXED2))
    monkeypatch.setitem(policies.MODES, "reader", policies.ByEffect(
        effects=frozenset({"read"}),
        denied_domains=frozenset({"browser", "factorio"}),
        denied_pairs=_pairs("operations.party_list") | _CARDS,
        pairs=_pairs("operations.board_get", "operations.board_list", "operations.board_complete",
                     "operations.board_fail", "operations.board_message_post",
                     "operations.board_messages", "reflection.compact", "reflection.whoami",
                     "search.query"),
        when={("operations", "board_post"): _is_reply}))



@pytest.fixture
def client():
    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture
def dispatched(monkeypatch):
    """Record what reaches the catalog instead of running it."""
    calls: list[tuple[str, dict, str | None]] = []

    async def fake(name, args, as_=None):
        calls.append((name, args, as_))
        return "ok"

    monkeypatch.setattr(server.catalog, "dispatch", fake)
    return calls


@pytest.fixture
def modes(monkeypatch):
    """Answer `mode_of` from a dict, count the lookups, start with a clean cache."""
    from awm.claudedaemon import sessionmode

    answers: dict[int, object] = {}
    lookups: list[int] = []

    def fake(pid):
        lookups.append(pid)
        answer = answers[pid]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(sessionmode, "mode_of", fake)
    monkeypatch.setattr(server, "_mode_cache", {})
    answers["lookups"] = lookups  # type: ignore[assignment]
    return answers


def invoke(client, name, args=None, headers=HEAD):
    return client.post("/invoke", json={"name": name, "args": args or {}}, headers=headers)


def domain_call(client, domain, verb, inner=None, *, peer=None, headers=HEAD):
    args = {"verb": verb, "args": inner or {}}
    if peer:
        args["peer"] = peer
    return invoke(client, domain, args, headers)


# --- the decision, per mode ------------------------------------------------------


FIXED_ALLOWED = [
    ("operations", "board_list"), ("operations", "board_get"), ("operations", "board_fail"),
    ("reflection", "start_other"), ("reflection", "list_other"), ("reflection", "compact"),
    ("operations", "status"), ("operations", "queue_list"), ("operations", "queue_get"),
    ("operations", "queue_assign"), ("journal", "fetch"), ("scope", "search"),
    ("journal", "goal_read"), ("operations", "board_messages"),
]
FIXED_REFUSED = [
    ("reflection", "stop_other"), ("reflection", "claim_other"), ("reflection", "seed_other"),
    ("reflection", "remove_other"),
    ("ssh", "connect"), ("social", "send"), ("journal", "post"), ("scope", "create"),
    ("scope", "delete"), ("journal", "goal_set"), ("operations", "party_add"),
    ("operations", "party_list"), ("reflection", "message_self"), ("reflection", "mode"),
    ("operations", "ops_post"), ("spawn", "start"), ("awm", "restart"),
    ("operations", "board_post"), ("operations", "board_claim"),
    ("operations", "board_complete"), ("operations", "board_message_post"),
    ("journal", "render"),
]


@pytest.mark.parametrize("domain,verb", FIXED_ALLOWED)
def test_a_fixed_mode_may_call_its_verbs(client, dispatched, modes, domain, verb):
    modes[PID] = "fixed"
    assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, f"{domain}_{verb}").status_code == 200
    assert len(dispatched) == 2


@pytest.mark.parametrize("domain,verb", FIXED_REFUSED)
def test_a_fixed_mode_is_refused_everything_else(client, dispatched, modes, domain, verb):
    modes[PID] = "fixed"
    for resp in (domain_call(client, domain, verb), invoke(client, f"{domain}_{verb}")):
        assert resp.status_code == 403
        assert "fixed" in resp.json()["detail"]
    assert dispatched == []


FIXED2_ALLOWED = [("reflection", "start_other"), ("reflection", "list_other"),
                     ("reflection", "stop_other"), ("operations", "board_list"),
                     ("operations", "board_get"), ("operations", "status"),
                     ("operations", "queue_list"), ("operations", "queue_get"),
                     ("reflection", "compact"), ("journal", "fetch")]
FIXED2_REFUSED = [("operations", "board_post"), ("operations", "board_claim"),
                     ("operations", "board_complete"), ("operations", "board_fail"),
                     ("operations", "queue_assign"), ("reflection", "claim_other"),
                     ("journal", "post"), ("reflection", "message_self")]


@pytest.mark.parametrize("domain,verb", FIXED2_ALLOWED)
def test_a_second_fixed_mode_may_call_its_verbs(client, dispatched, modes, domain, verb):
    modes[PID] = "fixed2"
    assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, f"{domain}_{verb}").status_code == 200


@pytest.mark.parametrize("domain,verb", FIXED2_REFUSED)
def test_a_second_fixed_mode_never_acts_on_cards(client, dispatched, modes, domain, verb):
    modes[PID] = "fixed2"
    assert domain_call(client, domain, verb).status_code == 403
    assert invoke(client, f"{domain}_{verb}").status_code == 403
    assert dispatched == []


def test_an_unknown_mode_may_only_compact(client, dispatched, modes):
    modes[PID] = "unknown"
    assert domain_call(client, "reflection", "compact").status_code == 200
    for domain, verb in FIXED_ALLOWED + FIXED2_ALLOWED:
        if (domain, verb) == ("reflection", "compact"):
            continue
        assert domain_call(client, domain, verb).status_code == 403, (domain, verb)
    assert [c[0] for c in dispatched] == ["reflection"]


def test_a_session_that_is_positively_not_cx_started_is_not_gated(client, dispatched, modes):
    modes[PID] = None
    for domain, verb in FIXED_REFUSED:
        assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, "ssh_connect").status_code == 200


def test_a_worker_is_not_gated(client, dispatched, modes):
    modes[PID] = "worker"
    assert domain_call(client, "journal", "post").status_code == 200
    assert invoke(client, "reflection_stop_other").status_code == 200


def test_describe_is_allowed_for_a_domain_the_mode_can_use(client, dispatched, modes):
    modes[PID] = "fixed"
    assert domain_call(client, "operations", "describe").status_code == 200
    assert domain_call(client, "ssh", "describe").status_code == 403
    modes[PID] = "unknown"
    server._mode_cache.clear()
    assert domain_call(client, "reflection", "describe").status_code == 200
    assert domain_call(client, "operations", "describe").status_code == 403


def test_a_restricted_mode_may_not_name_a_peer(client, dispatched, modes):
    modes[PID] = "fixed"
    resp = domain_call(client, "operations", "board_list", peer="capella")
    assert resp.status_code == 403 and "peer" in resp.json()["detail"]
    assert invoke(client, "operations_board_list", {"peer": "capella"}).status_code == 403
    assert dispatched == []


def test_a_call_with_no_verb_is_refused(client, dispatched, modes):
    modes[PID] = "fixed"
    for name in ("operations", "providersOf", "more"):
        assert invoke(client, name, {"tool": "operations"}).status_code == 403
    assert dispatched == []


def test_a_flat_name_that_also_carries_a_verb_is_judged_as_the_flat_call_dispatch_makes(
        client, dispatched, modes, catalog_of_services):
    modes[PID] = "fixed"
    # dispatch takes the flat path when the name is a known tool, so a stray
    # `verb` argument cannot turn operations_board_list into another verb
    assert invoke(client, "operations_board_list", {"verb": "list"}).status_code == 200
    assert invoke(client, "operations_board_list", {}).status_code == 200


def test_an_unknown_domain_with_a_verb_is_read_as_a_domain_call(client, dispatched, modes):
    modes[PID] = "fixed"
    assert domain_call(client, "operations", "board_list").status_code == 200
    assert domain_call(client, "elsewhere", "list").status_code == 403


def test_a_malformed_args_bag_is_refused(client, dispatched, modes):
    modes[PID] = "fixed"
    assert server._call_refusal("fixed", "operations_board_list", ["x"]) is not None


def test_the_caller_stamp_still_reaches_an_allowed_call(client, dispatched, modes):
    modes[PID] = "fixed"
    invoke(client, "reflection", {"verb": "compact", "args": {"_caller_pid": 1}})
    assert dispatched[0][1]["args"]["_caller_pid"] == PID


# --- who is asking ---------------------------------------------------------------


def test_a_request_with_no_pid_header_is_not_gated(client, dispatched, modes):
    assert invoke(client, "ssh_connect", headers={}).status_code == 200
    assert modes["lookups"] == []


def test_an_edge_stamped_request_is_not_gated_here(client, dispatched, modes):
    headers = {**HEAD, "X-Awm-As": "peer:capella"}
    assert invoke(client, "ssh_connect", headers=headers).status_code == 200
    assert modes["lookups"] == []


def test_the_descendant_header_is_resolved_like_the_stamp(client, dispatched, modes,
                                                           monkeypatch):
    monkeypatch.setattr(server.mcp_caller, "resolve_caller_pid", lambda pid: {4242: PID}[pid])
    modes[PID] = "fixed"
    resp = invoke(client, "ssh_connect", headers={"X-Awm-Caller-Pid": "4242"})
    assert resp.status_code == 403
    assert modes["lookups"] == [PID]


def test_the_session_header_wins_over_the_descendant_header(client, dispatched, modes,
                                                            monkeypatch):
    monkeypatch.setattr(server.mcp_caller, "resolve_caller_pid", lambda pid: 1 / 0)
    modes[PID] = "fixed"
    headers = {**HEAD, "X-Awm-Caller-Pid": "4242"}
    assert invoke(client, "operations_board_list", headers=headers).status_code == 200


def test_a_non_ascii_digit_header_is_the_most_restricted_mode():
    assert server._caller_mode({"X-Awm-Session-Pid": "²"}, None) == "unknown"


@pytest.mark.parametrize("raw", ["abc", "12x", "-3"])
def test_a_malformed_pid_header_is_the_most_restricted_mode(client, dispatched, modes, raw):
    assert invoke(client, "operations_board_list", headers={"X-Awm-Session-Pid": raw}).status_code == 403
    assert invoke(client, "reflection_compact",
                  headers={"X-Awm-Session-Pid": raw}).status_code == 200


def test_a_failed_descendant_walk_is_the_most_restricted_mode(client, dispatched, modes,
                                                              monkeypatch):
    monkeypatch.setattr(server.mcp_caller, "resolve_caller_pid", lambda pid: 1 / 0)
    assert invoke(client, "operations_board_list",
                  headers={"X-Awm-Caller-Pid": "4242"}).status_code == 403


@pytest.mark.parametrize("failure", [OSError("disk"), ValueError("bad json"), RuntimeError("x")])
def test_a_failed_lookup_is_the_most_restricted_mode(client, dispatched, modes, failure):
    modes[PID] = failure
    assert invoke(client, "operations_board_list").status_code == 403
    assert invoke(client, "reflection_compact").status_code == 200


def test_a_missing_claudedaemon_is_the_most_restricted_mode(client, dispatched, modes,
                                                            monkeypatch):
    import builtins
    real = builtins.__import__

    def refuse(name, *a, **kw):
        if name == "awm.claudedaemon" or name.startswith("awm.claudedaemon."):
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", refuse)
    monkeypatch.delitem(__import__("sys").modules, "awm.claudedaemon.sessionmode", raising=False)
    assert server._mode_of_pid(PID) == "unknown"


# --- the cache -------------------------------------------------------------------


def test_the_mode_is_looked_up_once_per_pid_inside_the_window(client, dispatched, modes):
    modes[PID] = "fixed"
    for _ in range(3):
        invoke(client, "operations_board_list")
    assert modes["lookups"] == [PID]


def test_the_cache_is_per_pid(client, dispatched, modes):
    modes[PID] = "fixed"
    modes[PID + 1] = None
    invoke(client, "ssh_connect")
    assert invoke(client, "ssh_connect",
                  headers={"X-Awm-Session-Pid": str(PID + 1)}).status_code == 200
    assert invoke(client, "ssh_connect").status_code == 403
    assert sorted(modes["lookups"]) == [PID, PID + 1]


def test_a_lookup_is_repeated_once_the_window_has_passed(client, dispatched, modes, monkeypatch):
    modes[PID] = "fixed"
    invoke(client, "operations_board_list")
    monkeypatch.setattr(server, "_MODE_CACHE_TTL_S", 0.0)
    invoke(client, "operations_board_list")
    assert modes["lookups"] == [PID, PID]


def test_the_cache_stays_bounded(modes, monkeypatch):
    monkeypatch.setattr(server, "_MODE_CACHE_MAX", 4)
    for pid in range(10, 30):
        modes[pid] = None
        server._mode_of_pid(pid)
    assert len(server._mode_cache) <= 4


# --- the /svc door ---------------------------------------------------------------


class _Registry:
    def __init__(self, rec):
        self.rec = rec

    def is_empty(self):
        return False

    def longest_match(self, path):
        return self.rec


def _service(name, functions):
    from awm.gateway.hub.registry import ServiceRecord

    return ServiceRecord(name=name, prefix=f"/svc/{name}", kind="service",
                         api={"functions": functions})


@pytest.fixture
def door(monkeypatch):
    """POST to a registered service; a call that clears the gate reaches the
    proxy, which answers 503 because no control channel is open."""
    def post(service, fn, headers=HEAD, body=None, path=None):
        monkeypatch.setattr(server, "_get_hub_registry", lambda: _Registry(service))
        client = TestClient(server.app, raise_server_exceptions=False)
        return client.post(path or f"/svc/{service.name}/fn/{fn}",
                           json=body or {}, headers=headers)

    return post


def test_the_door_gates_a_function_by_its_tool_name(door, modes):
    modes[PID] = "fixed"
    board = _service("board", [{"name": "fail", "tool": "operations_board_fail"},
                               {"name": "party_add", "tool": "operations_party_add"}])
    assert door(board, "fail").status_code == 503  # past the gate
    refused = door(board, "party_add")
    assert refused.status_code == 403 and "operations.party_add" in refused.json()["error"]


def test_the_door_follows_a_tool_name_override(door, modes):
    modes[PID] = "fixed"
    scopes = _service("scopes", [
        {"name": "awm_fetch", "tool": "journal_fetch"},
        {"name": "scope_create", "tool": "scope_create"},
        {"name": "scope_post", "tool": "journal_post"},
    ])
    assert door(scopes, "awm_fetch").status_code == 503
    assert door(scopes, "scope_create").status_code == 403
    assert door(scopes, "scope_post").status_code == 403


def test_the_door_refuses_an_unregistered_function(door, modes):
    modes[PID] = "fixed"
    assert door(_service("board", []), "party_add").status_code == 403


def test_the_door_applies_the_unknown_mode(door, modes):
    modes[PID] = "unknown"
    reflection = _service("reflection", [{"name": "compact"},
                                         {"name": "send", "tool": "reflection_message_self"}])
    board = _service("board", [{"name": "list", "tool": "operations_board_list"}])
    assert door(reflection, "compact").status_code == 503
    assert door(reflection, "send").status_code == 403
    assert door(board, "list").status_code == 403


def test_the_door_closes_every_other_service_path_to_a_restricted_mode(door, modes):
    modes[PID] = "fixed2"
    board = _service("board", [{"name": "list", "tool": "operations_board_list"}])
    assert door(board, "x", path="/svc/board/session/pcm").status_code == 403


def test_the_door_does_not_gate_an_ungated_session(door, modes):
    modes[PID] = None
    board = _service("board", [{"name": "party_add", "tool": "operations_party_add"}])
    assert door(board, "party_add").status_code == 503
    assert door(board, "party_add", headers={}).status_code == 503


def test_the_door_does_not_gate_an_edge_stamped_request(door, modes, domestic_peers):
    board = _service("board", [{"name": "party_add", "tool": "operations_party_add"}])
    resp = door(board, "party_add", headers={**HEAD, "X-Awm-As": "peer:capella"})
    assert resp.status_code == 503 and modes["lookups"] == []


# --- the real tri-state lookup, end to end ---------------------------------------


def _proc_start(pid: int) -> str:
    with open(f"/proc/{pid}/stat") as fh:
        return fh.read().partition(") ")[2].split()[19]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary Claude home, laid out as cx and the gateway both read it."""
    root = tmp_path / "claude"
    for sub in ("daemon", "jobs", "sessions", "cx/starts"):
        (root / sub).mkdir(parents=True)
    (root / "daemon" / "roster.json").write_text(json.dumps({"workers": {}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    for var in ("AWM_CX_ROSTER", "AWM_CX_JOBS", "AWM_CX_SESSIONS", "AWM_CX_STATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "_mode_cache", {})
    # the tests run under a real Claude Code session; its processes are not
    # candidates for the ancestor lookup unless a test says so
    from awm.claudedaemon import sessionmode

    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: False)
    return root


def _start_session(root, mode, job="abababab"):
    """A background session the way cx records it: roster, job state, lineage."""
    (root / "daemon" / "roster.json").write_text(json.dumps({"workers": {job: {
        "replPid": PID, "replProcStart": _proc_start(PID), "startedAt": 1,
        "dispatch": {"seed": {"name": "rep"}}}}}))
    (root / "jobs" / job).mkdir()
    (root / "jobs" / job / "state.json").write_text(json.dumps({"name": "rep"}))
    (root / "cx" / "starts" / f"{job}.json").write_text(json.dumps({"mode": mode}))


def test_a_cx_started_fixed_mode_session_is_gated_from_disk(client, dispatched, home):
    _start_session(home, "fixed")
    assert invoke(client, "operations_board_list").status_code == 200
    assert invoke(client, "operations_party_add").status_code == 403
    assert domain_call(client, "ssh", "connect").status_code == 403


def test_the_gateway_reads_the_lineage_where_cx_writes_it(client, dispatched, home, monkeypatch):
    state = home.parent / "elsewhere"
    (state / "starts").mkdir(parents=True)
    (home / "cx" / "starts" / "abababab.json").write_text("{}")
    monkeypatch.setenv("AWM_CX_STATE", str(state))
    (state / "starts" / "abababab.json").write_text(json.dumps({"mode": "fixed2"}))
    _start_session(home, "worker")
    (state / "starts" / "abababab.json").write_text(json.dumps({"mode": "fixed2"}))
    assert invoke(client, "operations_board_post").status_code == 403


def test_a_pid_with_no_record_is_not_gated(client, dispatched, home):
    assert invoke(client, "ssh_connect").status_code == 200


def test_an_unreadable_roster_leaves_only_compaction(client, dispatched, home, monkeypatch):
    from awm.claudedaemon import sessionmode

    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
    _start_session(home, "fixed")
    (home / "daemon" / "roster.json").write_text("{ not json")
    assert invoke(client, "operations_board_list").status_code == 403
    assert invoke(client, "reflection_compact").status_code == 200


def test_a_corrupt_lineage_record_leaves_only_compaction(client, dispatched, home):
    _start_session(home, "fixed")
    (home / "cx" / "starts" / "abababab.json").write_text("{ not json")
    assert invoke(client, "operations_board_list").status_code == 403
    assert invoke(client, "reflection_compact").status_code == 200


def test_an_unrestricted_recorded_mode_is_not_gated(client, dispatched, home):
    _start_session(home, "worker")
    assert invoke(client, "ssh_connect").status_code == 200


def test_an_interactive_caller_is_ungated_whatever_else_is_damaged(client, dispatched, home):
    """The lockout: a damaged roster or a missing lineage directory must not
    turn every terminal into `unknown`."""
    import shutil

    (home / "sessions" / f"{PID}.json").write_text(json.dumps({
        "pid": PID, "procStart": _proc_start(PID), "kind": "interactive"}))
    (home / "daemon" / "roster.json").write_text("{ not json")
    shutil.rmtree(home / "cx")
    assert invoke(client, "ssh_connect").status_code == 200
    assert invoke(client, "operations_party_add").status_code == 200


def test_a_caller_with_no_roster_and_no_lineage_directory_is_ungated(client, dispatched, home):
    import shutil

    (home / "daemon" / "roster.json").unlink()
    shutil.rmtree(home / "cx")
    assert invoke(client, "ssh_connect").status_code == 200


def test_a_corrupt_record_of_another_job_does_not_lock_the_session(client, dispatched,
                                                                       home):
    _start_session(home, "fixed")
    (home / "cx" / "starts" / "cccccccc.json").write_text("{ not json")
    (home / "cx" / "starts" / "pending-0123456789abcdef.json").write_text("[")
    assert invoke(client, "operations_board_list").status_code == 200
    assert invoke(client, "operations_board_post").status_code == 403


def test_a_proxy_whose_repl_has_no_session_record_is_gated_by_the_roster(
        client, dispatched, home, monkeypatch):
    """The REPL of a cx-started session has no sessions/<pid>.json, and its MCP
    proxy runs under a wrapper: the caller is a record-less child of a listed job."""
    from awm.claudedaemon import sessionmode

    parent = os.getppid()
    (home / "daemon" / "roster.json").write_text(json.dumps({"workers": {"abababab": {
        "replPid": parent, "replProcStart": _proc_start(parent),
        "dispatch": {"seed": {"name": "rep"}}}}}))
    (home / "cx" / "starts" / "abababab.json").write_text(json.dumps({"mode": "reader"}))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: pid == parent)
    assert invoke(client, "scope_post").status_code == 403
    assert invoke(client, "reflection_compact").status_code == 200


def test_a_header_naming_a_pid_with_no_process_is_the_most_restricted_mode(client, dispatched,
                                                                          home):
    """The T14 fail-open: pid 4999999 has no /proc entry, and the walk up from it
    used to end at once and read as "not cx-started"."""
    dead = {"X-Awm-Session-Pid": "4999999"}
    assert not os.path.exists("/proc/4999999")
    assert invoke(client, "scope_post", headers=dead).status_code == 403
    assert domain_call(client, "scope", "post", headers=dead).status_code == 403
    assert invoke(client, "reflection_compact", headers=dead).status_code == 200
    descendant = {"X-Awm-Caller-Pid": "4999999"}
    assert invoke(client, "scope_post", headers=descendant).status_code == 403
    assert dispatched and all(c[0] == "reflection_compact" for c in dispatched)


def test_a_pid_that_exists_but_is_interactive_stays_ungated(client, dispatched, home):
    (home / "sessions" / f"{PID}.json").write_text(json.dumps({
        "pid": PID, "procStart": _proc_start(PID), "kind": "interactive"}))
    assert invoke(client, "scope_post").status_code == 200
    assert invoke(client, "scope_post",
                  headers={"X-Awm-Caller-Pid": str(PID)}).status_code == 200


def test_a_cx_launched_job_with_no_lineage_is_the_most_restricted_mode(client, dispatched, home):
    _start_session(home, "worker")
    (home / "cx" / "starts" / "abababab.json").unlink()
    roster_path = home / "daemon" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["abababab"]["dispatch"]["launch"] = {"args": ["--permission-mode=dontAsk"]}
    roster_path.write_text(json.dumps(data))
    assert invoke(client, "operations_board_list").status_code == 403
    assert invoke(client, "reflection_compact").status_code == 200


# --- an effect-based mode ----------------------------------------


def _fn(name, effect=None, tool=None):
    spec = {"name": name}
    if effect:
        spec["effect"] = effect
    if tool:
        spec["tool"] = tool
    return spec


@pytest.fixture
def catalog_of_services(monkeypatch):
    """A catalog whose verbs declare effects, as the services do."""
    ops = "operations_"
    records = [
        _service("board", [_fn("post", "queue", tool=ops + "board_post"),
                           _fn("list", "read", tool=ops + "board_list"),
                           _fn("get", "read", tool=ops + "board_get"),
                           _fn("complete", "queue", tool=ops + "board_complete"),
                           _fn("fail", "queue", tool=ops + "board_fail"),
                           _fn("claim", "queue", tool=ops + "board_claim"),
                           _fn("party_list", "read", tool=ops + "party_list"),
                           _fn("party_add", "secret", tool=ops + "party_add")]),
        _service("journal", [_fn("post", "write"), _fn("fetch", "read")]),
        _service("scope", [_fn("create", "write"), _fn("sync")]),
        _service("precedence", [_fn("search", "read"), _fn("add", "write")]),
        _service("cx", [_fn("list", "read", tool="reflection_list_other"),
                        _fn("start", "write", tool="reflection_start_other"),
                        _fn("stop", "write", tool="reflection_stop_other")]),
        _service("reporting", [_fn("card_list", "read", tool=ops + "queue_list"),
                               _fn("card_assign", "write", tool=ops + "queue_assign")]),
        _service("rlm-browser", [_fn("cdp", "read", tool="browser_cdp"),
                                 _fn("open", "write", tool="browser_open")]),
        _service("reflection", [_fn("compact"),
                                _fn("send", tool="reflection_message_self")]),
        _service("auth", [_fn("token", "secret", tool="awm_token")]),
    ]

    class Registry:
        def service_records(self):
            return list(records)

    monkeypatch.setattr(server.catalog, "get_registry", lambda: Registry())
    return records


READER_ALLOWED = [("reflection", "compact"), ("reflection", "whoami"), ("precedence", "search"),
                    ("reflection", "list_other"), ("journal", "fetch"),
                    ("operations", "board_get"), ("operations", "board_list"),
                    ("operations", "board_complete"), ("operations", "board_fail")]
READER_REFUSED = [("journal", "post"), ("scope", "create"), ("scope", "sync"),
                    ("precedence", "add"), ("reflection", "start_other"),
                    ("reflection", "stop_other"), ("operations", "queue_list"),
                    ("operations", "queue_assign"), ("browser", "cdp"), ("browser", "open"),
                    ("operations", "board_claim"), ("operations", "party_list"),
                    ("operations", "party_add"), ("reflection", "message_self"),
                    ("reflection", "mode"), ("awm", "token"), ("nowhere", "verb")]


@pytest.mark.parametrize("domain,verb", READER_ALLOWED)
def test_a_reader_may_call_what_declares_read_and_finish_its_card(
        client, dispatched, modes, catalog_of_services, domain, verb):
    modes[PID] = "reader"
    assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, f"{domain}_{verb}").status_code == 200


@pytest.mark.parametrize("domain,verb", READER_REFUSED)
def test_a_reader_is_refused_everything_else(client, dispatched, modes,
                                               catalog_of_services, domain, verb):
    modes[PID] = "reader"
    assert domain_call(client, domain, verb).status_code == 403
    assert invoke(client, f"{domain}_{verb}").status_code == 403
    assert dispatched == []


def test_a_reader_posts_a_reply_message_and_nothing_else(client, dispatched, modes,
                                                           catalog_of_services):
    modes[PID] = "reader"
    reply = {"kind": "message", "recipient": "mock", "title": "t", "body": "b",
             "reply_to": "a" * 32}
    assert domain_call(client, "operations", "board_post", reply).status_code == 200
    assert invoke(client, "operations_board_post", reply).status_code == 200
    for bad in ({**reply, "kind": "request"}, {k: v for k, v in reply.items() if k != "reply_to"}):
        assert domain_call(client, "operations", "board_post", bad).status_code == 403
        assert invoke(client, "operations_board_post", bad).status_code == 403
    assert len(dispatched) == 2


def test_a_reader_may_not_name_a_peer(client, dispatched, modes, catalog_of_services):
    modes[PID] = "reader"
    assert domain_call(client, "precedence", "search", peer="capella").status_code == 403


def test_the_door_judges_a_reader_by_the_declared_effect(door, modes):
    modes[PID] = "reader"
    kb = _service("kb", [_fn("search", "read"), _fn("add", "write"), _fn("odd")])
    assert door(kb, "search").status_code == 503
    assert door(kb, "add").status_code == 403
    assert door(kb, "odd").status_code == 403
    browser = _service("rlm-browser", [_fn("cdp", "read", tool="browser_cdp")])
    assert door(browser, "cdp").status_code == 403
    queue = _service("reporting", [_fn("card_list", "read", tool="operations_queue_list")])
    assert door(queue, "card_list").status_code == 403


def test_the_door_refuses_a_reader_post_it_cannot_inspect(door, modes):
    modes[PID] = "reader"
    board = _service("board", [_fn("post", "queue", tool="operations_board_post"),
                               _fn("fail", "queue", tool="operations_board_fail")])
    assert door(board, "post", body={"kind": "message", "reply_to": "a" * 32}).status_code == 403
    assert door(board, "fail").status_code == 503


# --- gateway-native ops are judged by what they resolve to ---------------------------


@pytest.mark.parametrize("mode", ["fixed", "fixed2", "unknown", "reader"])
@pytest.mark.parametrize("name", ["awm_restart", "awm_mcp_sync"])
def test_a_flat_native_op_is_judged_as_its_tool_and_verb(client, dispatched, modes,
                                                             catalog_of_services, mode, name):
    modes[PID] = mode
    assert invoke(client, name).status_code == 403
    assert dispatched == []


def test_the_native_op_resolves_to_its_tool_and_verb(catalog_of_services):
    domain, verb, effect, _, flat = server._resolve_call("awm_restart", {})
    assert (domain, verb, effect, flat) == ("awm", "restart", "write", True)
    assert server._resolve_call("awm", {"verb": "status"})[:3] == ("awm", "status", "read")
    assert server._resolve_call("operations_peer_join", {})[:3] == (
        "operations", "peer_join", "write")


def test_a_native_read_op_is_open_to_the_reader_only_by_its_effect(
        client, dispatched, modes, catalog_of_services):
    modes[PID] = "reader"
    assert invoke(client, "awm_status").status_code == 200  # awm.status declares read
    modes[PID] = "fixed"
    server._mode_cache.clear()
    assert invoke(client, "awm_status").status_code == 403


def test_a_flat_describe_through_a_native_name_is_still_refused(client, dispatched, modes):
    modes[PID] = "fixed"
    assert invoke(client, "awm_describe").status_code == 403


# --- malformed requests -------------------------------------------------------------


@pytest.mark.parametrize("name", ["reflection", "reflection_compact", "cx", "cx_start",
                                  "board_list"])
@pytest.mark.parametrize("args", [["x"], "text", 5, True, [], 0, ""])
def test_a_non_object_args_is_a_400_before_stamping_or_the_gate(client, dispatched, modes,
                                                                name, args):
    modes[PID] = "fixed"
    resp = client.post("/invoke", json={"name": name, "args": args}, headers=HEAD)
    assert resp.status_code == 400 and "args" in resp.json()["detail"]
    assert dispatched == [] and modes["lookups"] == []


def test_an_edge_stamped_non_object_args_is_left_to_the_catalog_gate(client, monkeypatch):
    seen = []

    async def refuse(name, args, as_=None):
        seen.append((name, args, as_))
        raise ValueError(f"Unknown tool: {name}")

    monkeypatch.setattr(server.catalog, "dispatch", refuse)
    resp = client.post("/invoke", json={"name": "reflection", "args": [1]},
                       headers={"X-Awm-As": "peer:capella"})
    assert resp.status_code == 404 and seen == [("reflection", [1], "peer:capella")]


@pytest.mark.parametrize("payload", [["x"], "text", 5, None])
def test_a_non_object_payload_is_rejected(client, dispatched, payload):
    assert client.post("/invoke", json=payload, headers=HEAD).status_code in (400, 422)
    assert dispatched == []


@pytest.mark.parametrize("name", [5, ["a"], {"a": 1}, True])
def test_a_non_string_name_is_a_400(client, dispatched, name):
    resp = client.post("/invoke", json={"name": name, "args": {}}, headers=HEAD)
    assert resp.status_code == 400
    assert dispatched == []


def test_missing_or_null_args_still_dispatch(client, dispatched, modes):
    modes[PID] = None
    assert client.post("/invoke", json={"name": "cx_list"}, headers=HEAD).status_code == 200
    assert client.post("/invoke", json={"name": "cx_list", "args": None},
                       headers=HEAD).status_code == 200
    assert dispatched[0][1] == {} and dispatched[1][1] == {}


# --- small refusals ---------------------------------------------------------------


def test_a_flat_describe_name_is_not_a_describe(client, dispatched, modes):
    modes[PID] = "fixed"
    assert invoke(client, "operations_describe").status_code == 403
    modes[PID] = "reader"
    server._mode_cache.clear()
    assert invoke(client, "kb_describe").status_code == 403
    assert domain_call(client, "kb", "describe").status_code == 200


def test_a_restricted_mode_gets_403_not_a_peer_redirect(client, monkeypatch, modes):
    from awm.gateway import peer_catalog

    async def redirect(name, args, as_=None):
        raise peer_catalog.PeerRedirect("capella", name, "list")

    monkeypatch.setattr(server.catalog, "dispatch", redirect)
    modes[PID] = "fixed"
    resp = invoke(client, "operations", {"verb": "board_list"},
                  {**HEAD, "X-Awm-Peer-Redirect": "1"})
    assert resp.status_code == 403 and "peer" in resp.json()["detail"]
    modes[PID] = None
    server._mode_cache.clear()
    resp = invoke(client, "operations", {"verb": "board_list"},
                  {**HEAD, "X-Awm-Peer-Redirect": "1"})
    assert resp.status_code == 200 and "peer_redirect" in resp.json()


def test_the_lineage_directory_is_created_at_startup(awm_workspace, tmp_path, monkeypatch):
    state = tmp_path / "cxstate"
    monkeypatch.setenv("AWM_CX_STATE", str(state))
    with TestClient(server.app, raise_server_exceptions=False):
        assert (state / "starts").is_dir()


def test_the_proxy_walk_reads_session_records_where_the_gate_does(tmp_path, monkeypatch):
    from awm.gateway import mcp_caller

    sessions = tmp_path / "sess"
    sessions.mkdir()
    (sessions / "4242.json").write_text("{}")
    monkeypatch.setenv("AWM_CX_SESSIONS", str(sessions))
    assert mcp_caller.resolve_caller_pid(4242, ppid_of=lambda p: None,
                                         is_opencode=lambda p: False) == 4242
    assert mcp_caller.resolve_caller_pid(77, ppid_of={77: 4242}.get,
                                         is_opencode=lambda p: False) == 4242
    monkeypatch.setenv("AWM_CX_SESSIONS", str(tmp_path / "nowhere"))
    assert mcp_caller.resolve_caller_pid(77, ppid_of={77: 4242}.get,
                                         is_opencode=lambda p: False) == 77
