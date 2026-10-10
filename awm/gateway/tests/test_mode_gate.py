"""The session mode gate on `/invoke` and the `/svc/<svc>/fn/<fn>` door.

A session cx started in a restricted mode may call only the verbs
`awm.config.modes` lists for it. The gateway finds the mode from the pid header
through `awm.claudedaemon.sessionmode`, which reads files on disk. These tests
cover the gate's decision for each mode, both call shapes and both doors, the
fail-closed lookups, and the per-pid cache. The last group runs the real
tri-state lookup against a temporary Claude home.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.hub, pytest.mark.smoke]

from awm.gateway import server

PID = os.getpid()
HEAD = {"X-Awm-Session-Pid": str(PID)}


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


REP_ALLOWED = [
    ("board", "list"), ("board", "get"), ("board", "fail"), ("cx", "start"),
    ("cx", "list"), ("reflection", "compact"), ("door", "status"), ("door", "list"),
    ("door", "get"), ("door", "assign"), ("scope", "fetch"), ("scope", "search"),
    ("scope", "goal_read"),
]
REP_REFUSED = [
    ("cx", "stop"), ("cx", "claim"), ("cx", "seed"), ("cx", "remove"),
    ("ssh", "connect"), ("social", "send"), ("scope", "post"), ("scope", "create"),
    ("scope", "delete"), ("scope", "goal_set"), ("board", "party_add"),
    ("board", "party_list"), ("reflection", "send"), ("reflection", "mode"),
    ("door", "purge"), ("spawn", "start"), ("gateway", "restart"),
    ("board", "post"), ("board", "claim"), ("board", "complete"), ("scope", "refresh"),
]


@pytest.mark.parametrize("domain,verb", REP_ALLOWED)
def test_representative_may_call_its_verbs(client, dispatched, modes, domain, verb):
    modes[PID] = "representative"
    assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, f"{domain}_{verb}").status_code == 200
    assert len(dispatched) == 2


@pytest.mark.parametrize("domain,verb", REP_REFUSED)
def test_representative_is_refused_everything_else(client, dispatched, modes, domain, verb):
    modes[PID] = "representative"
    for resp in (domain_call(client, domain, verb), invoke(client, f"{domain}_{verb}")):
        assert resp.status_code == 403
        assert "representative" in resp.json()["detail"]
    assert dispatched == []


SECRETARY_ALLOWED = [("cx", "start"), ("cx", "list"), ("cx", "stop"), ("board", "list"),
                     ("board", "get"), ("door", "status"), ("door", "list"),
                     ("door", "get"), ("reflection", "compact"), ("scope", "fetch")]
SECRETARY_REFUSED = [("board", "post"), ("board", "claim"), ("board", "complete"),
                     ("board", "fail"), ("door", "assign"), ("cx", "claim"),
                     ("scope", "post"), ("reflection", "send")]


@pytest.mark.parametrize("domain,verb", SECRETARY_ALLOWED)
def test_secretary_may_call_its_verbs(client, dispatched, modes, domain, verb):
    modes[PID] = "secretary"
    assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, f"{domain}_{verb}").status_code == 200


@pytest.mark.parametrize("domain,verb", SECRETARY_REFUSED)
def test_secretary_never_acts_on_cards(client, dispatched, modes, domain, verb):
    modes[PID] = "secretary"
    assert domain_call(client, domain, verb).status_code == 403
    assert invoke(client, f"{domain}_{verb}").status_code == 403
    assert dispatched == []


def test_an_unknown_mode_may_only_compact(client, dispatched, modes):
    modes[PID] = "unknown"
    assert domain_call(client, "reflection", "compact").status_code == 200
    for domain, verb in REP_ALLOWED + SECRETARY_ALLOWED:
        if (domain, verb) == ("reflection", "compact"):
            continue
        assert domain_call(client, domain, verb).status_code == 403, (domain, verb)
    assert [c[0] for c in dispatched] == ["reflection"]


def test_a_session_that_is_positively_not_cx_started_is_not_gated(client, dispatched, modes):
    modes[PID] = None
    for domain, verb in REP_REFUSED:
        assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, "ssh_connect").status_code == 200


def test_a_worker_is_not_gated(client, dispatched, modes):
    modes[PID] = "worker"
    assert domain_call(client, "scope", "post").status_code == 200
    assert invoke(client, "cx_stop").status_code == 200


def test_describe_is_allowed_for_a_domain_the_mode_can_use(client, dispatched, modes):
    modes[PID] = "representative"
    assert domain_call(client, "board", "describe").status_code == 200
    assert domain_call(client, "ssh", "describe").status_code == 403
    modes[PID] = "unknown"
    server._mode_cache.clear()
    assert domain_call(client, "reflection", "describe").status_code == 200
    assert domain_call(client, "board", "describe").status_code == 403


def test_a_restricted_mode_may_not_name_a_peer(client, dispatched, modes):
    modes[PID] = "representative"
    resp = domain_call(client, "board", "list", peer="capella")
    assert resp.status_code == 403 and "peer" in resp.json()["detail"]
    assert invoke(client, "board_list", {"peer": "capella"}).status_code == 403
    assert dispatched == []


def test_a_call_with_no_verb_is_refused(client, dispatched, modes):
    modes[PID] = "representative"
    for name in ("board", "providersOf", "more"):
        assert invoke(client, name, {"tool": "board"}).status_code == 403
    assert dispatched == []


def test_a_flat_name_that_also_carries_a_verb_must_pass_as_both(client, dispatched, modes):
    modes[PID] = "representative"
    assert invoke(client, "board_list", {"verb": "list"}).status_code == 403
    assert invoke(client, "board_list", {}).status_code == 200


def test_a_malformed_args_bag_is_refused(client, dispatched, modes):
    modes[PID] = "representative"
    assert server._call_refusal("representative", "board_list", ["x"]) is not None


def test_the_caller_stamp_still_reaches_an_allowed_call(client, dispatched, modes):
    modes[PID] = "representative"
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
    modes[PID] = "representative"
    resp = invoke(client, "ssh_connect", headers={"X-Awm-Caller-Pid": "4242"})
    assert resp.status_code == 403
    assert modes["lookups"] == [PID]


def test_the_session_header_wins_over_the_descendant_header(client, dispatched, modes,
                                                            monkeypatch):
    monkeypatch.setattr(server.mcp_caller, "resolve_caller_pid", lambda pid: 1 / 0)
    modes[PID] = "representative"
    headers = {**HEAD, "X-Awm-Caller-Pid": "4242"}
    assert invoke(client, "board_list", headers=headers).status_code == 200


def test_a_non_ascii_digit_header_is_the_most_restricted_mode():
    assert server._caller_mode({"X-Awm-Session-Pid": "²"}, None) == "unknown"


@pytest.mark.parametrize("raw", ["abc", "12x", "-3"])
def test_a_malformed_pid_header_is_the_most_restricted_mode(client, dispatched, modes, raw):
    assert invoke(client, "board_list", headers={"X-Awm-Session-Pid": raw}).status_code == 403
    assert invoke(client, "reflection_compact",
                  headers={"X-Awm-Session-Pid": raw}).status_code == 200


def test_a_failed_descendant_walk_is_the_most_restricted_mode(client, dispatched, modes,
                                                              monkeypatch):
    monkeypatch.setattr(server.mcp_caller, "resolve_caller_pid", lambda pid: 1 / 0)
    assert invoke(client, "board_list",
                  headers={"X-Awm-Caller-Pid": "4242"}).status_code == 403


@pytest.mark.parametrize("failure", [OSError("disk"), ValueError("bad json"), RuntimeError("x")])
def test_a_failed_lookup_is_the_most_restricted_mode(client, dispatched, modes, failure):
    modes[PID] = failure
    assert invoke(client, "board_list").status_code == 403
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
    modes[PID] = "representative"
    for _ in range(3):
        invoke(client, "board_list")
    assert modes["lookups"] == [PID]


def test_the_cache_is_per_pid(client, dispatched, modes):
    modes[PID] = "representative"
    modes[PID + 1] = None
    invoke(client, "ssh_connect")
    assert invoke(client, "ssh_connect",
                  headers={"X-Awm-Session-Pid": str(PID + 1)}).status_code == 200
    assert invoke(client, "ssh_connect").status_code == 403
    assert sorted(modes["lookups"]) == [PID, PID + 1]


def test_a_lookup_is_repeated_once_the_window_has_passed(client, dispatched, modes, monkeypatch):
    modes[PID] = "representative"
    invoke(client, "board_list")
    monkeypatch.setattr(server, "_MODE_CACHE_TTL_S", 0.0)
    invoke(client, "board_list")
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
    modes[PID] = "representative"
    board = _service("board", [{"name": "fail"}, {"name": "party_add"}])
    assert door(board, "fail").status_code == 503  # past the gate
    refused = door(board, "party_add")
    assert refused.status_code == 403 and "board.party_add" in refused.json()["error"]


def test_the_door_follows_a_tool_name_override(door, modes):
    modes[PID] = "representative"
    scopes = _service("scopes", [
        {"name": "awm_fetch", "tool": "scope_fetch"},
        {"name": "scope_create", "tool": "scope_create"},
        {"name": "scope_post", "tool": "scope_post"},
    ])
    assert door(scopes, "awm_fetch").status_code == 503
    assert door(scopes, "scope_create").status_code == 403
    assert door(scopes, "scope_post").status_code == 403


def test_the_door_refuses_an_unregistered_function(door, modes):
    modes[PID] = "representative"
    assert door(_service("board", []), "party_add").status_code == 403


def test_the_door_applies_the_unknown_mode(door, modes):
    modes[PID] = "unknown"
    reflection = _service("reflection", [{"name": "compact"}, {"name": "send"}])
    board = _service("board", [{"name": "list"}])
    assert door(reflection, "compact").status_code == 503
    assert door(reflection, "send").status_code == 403
    assert door(board, "list").status_code == 403


def test_the_door_closes_every_other_service_path_to_a_restricted_mode(door, modes):
    modes[PID] = "secretary"
    board = _service("board", [{"name": "list"}])
    assert door(board, "x", path="/svc/board/session/pcm").status_code == 403


def test_the_door_does_not_gate_an_ungated_session(door, modes):
    modes[PID] = None
    board = _service("board", [{"name": "party_add"}])
    assert door(board, "party_add").status_code == 503
    assert door(board, "party_add", headers={}).status_code == 503


def test_the_door_does_not_gate_an_edge_stamped_request(door, modes):
    board = _service("board", [{"name": "party_add"}])
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
    return root


def _start_session(root, mode, job="abababab"):
    """A background session the way cx records it: roster, job state, lineage."""
    (root / "daemon" / "roster.json").write_text(json.dumps({"workers": {job: {
        "replPid": PID, "replProcStart": _proc_start(PID), "startedAt": 1,
        "dispatch": {"seed": {"name": "rep"}}}}}))
    (root / "jobs" / job).mkdir()
    (root / "jobs" / job / "state.json").write_text(json.dumps({"name": "rep"}))
    (root / "cx" / "starts" / f"{job}.json").write_text(json.dumps({"mode": mode}))


def test_a_cx_started_representative_is_gated_from_disk(client, dispatched, home):
    _start_session(home, "representative")
    assert invoke(client, "board_list").status_code == 200
    assert invoke(client, "board_party_add").status_code == 403
    assert domain_call(client, "ssh", "connect").status_code == 403


def test_the_gateway_reads_the_lineage_where_cx_writes_it(client, dispatched, home, monkeypatch):
    state = home.parent / "elsewhere"
    (state / "starts").mkdir(parents=True)
    (home / "cx" / "starts" / "abababab.json").write_text("{}")
    monkeypatch.setenv("AWM_CX_STATE", str(state))
    (state / "starts" / "abababab.json").write_text(json.dumps({"mode": "secretary"}))
    _start_session(home, "worker")
    (state / "starts" / "abababab.json").write_text(json.dumps({"mode": "secretary"}))
    assert invoke(client, "board_post").status_code == 403


def test_a_pid_with_no_record_is_not_gated(client, dispatched, home):
    assert invoke(client, "ssh_connect").status_code == 200


def test_an_unreadable_roster_leaves_only_compaction(client, dispatched, home):
    _start_session(home, "representative")
    (home / "daemon" / "roster.json").write_text("{ not json")
    assert invoke(client, "board_list").status_code == 403
    assert invoke(client, "reflection_compact").status_code == 200


def test_a_corrupt_lineage_record_leaves_only_compaction(client, dispatched, home):
    _start_session(home, "representative")
    (home / "cx" / "starts" / "abababab.json").write_text("{ not json")
    assert invoke(client, "board_list").status_code == 403
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
    assert invoke(client, "board_party_add").status_code == 200


def test_a_caller_with_no_roster_and_no_lineage_directory_is_ungated(client, dispatched, home):
    import shutil

    (home / "daemon" / "roster.json").unlink()
    shutil.rmtree(home / "cx")
    assert invoke(client, "ssh_connect").status_code == 200


def test_a_corrupt_record_of_another_job_does_not_lock_the_representative(client, dispatched,
                                                                       home):
    _start_session(home, "representative")
    (home / "cx" / "starts" / "cccccccc.json").write_text("{ not json")
    (home / "cx" / "starts" / "pending-0123456789abcdef.json").write_text("[")
    assert invoke(client, "board_list").status_code == 200
    assert invoke(client, "board_post").status_code == 403


def test_a_cx_launched_job_with_no_lineage_is_the_most_restricted_mode(client, dispatched, home):
    _start_session(home, "worker")
    (home / "cx" / "starts" / "abababab.json").unlink()
    roster_path = home / "daemon" / "roster.json"
    data = json.loads(roster_path.read_text())
    data["workers"]["abababab"]["dispatch"]["launch"] = {"args": ["--permission-mode=dontAsk"]}
    roster_path.write_text(json.dumps(data))
    assert invoke(client, "board_list").status_code == 403
    assert invoke(client, "reflection_compact").status_code == 200


# --- the delegate: a deny-style mode ---------------------------------------------


@pytest.mark.parametrize("domain,verb", [("board", "complete"), ("board", "post"),
                                         ("scope", "post"), ("kb", "search"),
                                         ("cx", "list"), ("reflection", "compact")])
def test_a_delegate_may_call_what_is_not_denied(client, dispatched, modes, domain, verb):
    modes[PID] = "delegate"
    assert domain_call(client, domain, verb).status_code == 200
    assert invoke(client, f"{domain}_{verb}").status_code == 200


@pytest.mark.parametrize("domain,verb", [("cx", "start"), ("cx", "stop"), ("ssh", "connect"),
                                         ("auth", "login"), ("vpn", "up"), ("social", "send"),
                                         ("gateway", "restart"), ("services", "stop"),
                                         ("board", "party_add"), ("reflection", "mode"),
                                         ("reflection", "send")])
def test_a_delegate_is_refused_what_is_denied(client, dispatched, modes, domain, verb):
    modes[PID] = "delegate"
    assert domain_call(client, domain, verb).status_code == 403
    assert invoke(client, f"{domain}_{verb}").status_code == 403
    assert dispatched == []


def test_a_delegate_may_not_name_a_peer(client, dispatched, modes):
    modes[PID] = "delegate"
    assert domain_call(client, "kb", "search", peer="capella").status_code == 403


def test_the_door_applies_the_delegate_deny_set(door, modes):
    modes[PID] = "delegate"
    kb = _service("kb", [{"name": "search"}])
    ssh = _service("ssh", [{"name": "connect"}])
    assert door(kb, "search").status_code == 503
    assert door(ssh, "connect").status_code == 403


# --- malformed requests -------------------------------------------------------------


@pytest.mark.parametrize("name", ["reflection", "reflection_compact", "cx", "cx_start",
                                  "board_list"])
@pytest.mark.parametrize("args", [["x"], "text", 5, True, [], 0, ""])
def test_a_non_object_args_is_a_400_before_stamping_or_the_gate(client, dispatched, modes,
                                                                name, args):
    modes[PID] = "representative"
    resp = client.post("/invoke", json={"name": name, "args": args}, headers=HEAD)
    assert resp.status_code == 400 and "args" in resp.json()["detail"]
    assert dispatched == [] and modes["lookups"] == []


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
    modes[PID] = "representative"
    assert invoke(client, "board_describe").status_code == 403
    modes[PID] = "delegate"
    server._mode_cache.clear()
    assert invoke(client, "kb_describe").status_code == 403
    assert domain_call(client, "kb", "describe").status_code == 200


def test_a_restricted_mode_gets_403_not_a_peer_redirect(client, monkeypatch, modes):
    from awm.gateway import peer_catalog

    async def redirect(name, args, as_=None):
        raise peer_catalog.PeerRedirect("capella", name, "list")

    monkeypatch.setattr(server.catalog, "dispatch", redirect)
    modes[PID] = "representative"
    resp = invoke(client, "board", {"verb": "list"},
                  {**HEAD, "X-Awm-Peer-Redirect": "1"})
    assert resp.status_code == 403 and "peer" in resp.json()["detail"]
    modes[PID] = None
    server._mode_cache.clear()
    resp = invoke(client, "board", {"verb": "list"}, {**HEAD, "X-Awm-Peer-Redirect": "1"})
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
