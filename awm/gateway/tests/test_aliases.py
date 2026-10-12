"""Old tool and verb names reach their renamed successors over ``/invoke``.

The rows come from the wave-1 branches. A row fires only when the old name is gone
and its target is registered; the session mode gate and the caller stamp judge the
call after the rewrite.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from awm.gateway import aliases, catalog, server, usage
from awm.gateway.hub.registry import ServiceRecord

from .test_service_manifest_effects import MANIFESTS

pytestmark = [pytest.mark.hub, pytest.mark.smoke]

PID = 4242
HEAD = {"X-Awm-Session-Pid": str(PID)}


class _Registry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


class _Channel:
    def __init__(self):
        self.calls = []

    async def call(self, fn, args, as_=None, timeout=None, grants=None):
        self.calls.append((fn, args))
        return {"ran": fn}


def _rec(name, **published):
    """``{internal name: (published name, extra manifest keys)}``."""
    fns = []
    for fn, spec in published.items():
        tool, extra = spec if isinstance(spec, tuple) else (spec, {})
        fns.append({"name": fn, "tool": tool, "description": f"{tool}.", **extra})
    return ServiceRecord(name=name, prefix=f"/svc/{name}", kind="service",
                         service_id=f"sid-{name}", api={"functions": fns})


_HIDDEN = {"surfaces": ["cli", "http"]}


@pytest.fixture()
def world(monkeypatch, awm_workspace):
    records = [
        _rec("cx", start=("reflection_start_other", {"effect": "write"}),
             stop=("reflection_stop_other", {"effect": "write"}),
             list=("reflection_list_other", {"effect": "read"})),
        _rec("reflection", compact="reflection_compact",
             send="reflection_message_self"),
        _rec("reporting",
             request_human="operations_request_human",
             card_list=("operations_queue_list", {"effect": "read"}),
             hook_plan=("operations_hook_plan", _HIDDEN)),
        _rec("board", post=("operations_board_post", {"effect": "queue"}),
             list=("operations_board_list", {"effect": "read"})),
        _rec("journal", post=("journal_post", {"effect": "write"}),
             fetch=("journal_fetch", {"effect": "read", "category": "journals"})),
    ]
    stub = _Registry(records)
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    ch = _Channel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    mode = {"value": None}
    monkeypatch.setattr(server, "_caller_mode", lambda headers, as_: mode["value"])
    ch.mode = mode
    return ch


@pytest.fixture()
def client():
    return TestClient(server.app, raise_server_exceptions=False)


def _invoke(client, name, args=None, headers=HEAD):
    return client.post("/invoke", json={"name": name, "args": args or {}}, headers=headers)


# -- the table ----------------------------------------------------------------


def test_the_published_renames_are_in_the_table():
    pairs = {(a.flat_old, a.flat_new) for a in aliases.ALIASES}
    expected = {
        ("reflection_send", "reflection_message_self"),
        ("cx_start", "reflection_start_other"), ("cx_list", "reflection_list_other"),
        ("cx_stop", "reflection_stop_other"), ("cx_claim", "reflection_claim_other"),
        ("cx_seed", "reflection_seed_other"), ("cx_remove", "reflection_remove_other"),
        ("board_post", "operations_board_post"), ("board_list", "operations_board_list"),
        ("board_party_add", "operations_party_add"),
        ("board_party_list", "operations_party_list"),
        ("reporting_status", "operations_status"),
        ("reporting_card_list", "operations_queue_list"),
        ("reporting_card_assign", "operations_queue_assign"),
        ("reporting_claim_task", "operations_task_claim"),
        ("reporting_hook_plan_approved", "operations_hook_plan_approved"),
        ("reporting_ops_post", "operations_ops_post"),
        ("reporting_board_ensure", "operations_board_ensure"),
        ("reporting_request_human_user", "operations_request_human"),
        ("reporting_request_human_review", "operations_request_human"),
        ("scope_post", "journal_post"), ("scope_goal_history", "journal_goal_history"),
        ("scope_subscribe", "journal_subscribe"),
        ("scope_data_mount", "dvc_mount"), ("scope_data_gc", "dvc_gc"),
        ("scope_data_status", "dvc_scope_status"),
        ("scope_gather", "scope_git_gather"), ("scope_scatter", "scope_git_scatter"),
        ("scope_refresh", "journal_render"), ("project_search", "scope_search"),
        ("project_create", "scope_create"),
        ("gateway_list", "awm_registry_list"), ("services_list", "awm_services_list"),
        ("services_reap", "awm_services_reap"), ("peer_providers", "awm_providers"),
        ("peer_grant", "operations_peer_grant"), ("config_set", "awm_config_set"),
        ("config_contracts", "awm_config_contracts"),
        ("auth_node_key", "awm_node_key"), ("auth_status", "awm_auth_status"),
        ("rlm_browser_acquire", "browser_acquire"),
        ("rlm_factorio_world_new", "factorio_world_new"),
        ("penpot_plugins_status", "penpot-plugins_status"),
    }
    assert expected <= pairs


def test_the_removed_verbs_have_no_row():
    old = {a.flat_old for a in aliases.ALIASES}
    assert "scope_archive_search" not in old and "kb_sweep" not in old


def test_the_two_request_human_rows_inject_their_kind():
    by = {a.old_verb: a.inject for a in aliases.ALIASES if a.old_tool == "reporting"}
    assert by["request_human_user"] == {"kind": "blocked"}
    assert by["request_human_review"] == {"kind": "review"}
    new = {a.flat_new for a in aliases.ALIASES if a.old_verb.startswith("request_human")}
    assert new == {"operations_request_human"}


def test_no_old_flat_name_is_claimed_twice():
    names = [a.flat_old for a in aliases.ALIASES]
    assert len(names) == len(set(names))


def _tree_published():
    from awm.gateway.gateway_ops import GATEWAY_OPERATIONS
    from awm.gateway.surface import published_name

    names = {op.name for op in GATEWAY_OPERATIONS}
    for service, entries in MANIFESTS.items():
        for e in entries:
            if isinstance(e["name"], str):
                names.add(published_name(service, {"name": e["name"], "tool": e.get("tool")}))
    return names


def test_every_row_this_branch_owns_lands_on_a_name_the_tree_publishes():
    published = _tree_published()
    owned = {"awm", "browser", "factorio", "penpot-plugins"}
    missing = [a.flat_new for a in aliases.ALIASES
               if a.new_tool in owned or a.flat_new.startswith("operations_peer_")
               if a.flat_new not in published]
    assert not missing


def test_usage_renames_fold_the_old_pair_into_the_new_one():
    renames = aliases.usage_renames()
    assert renames[("scope", "post")] == ("journal", "post")
    assert renames[("cx", "start")] == ("reflection", "start_other")
    assert renames[("rlm", "browser_status")] == ("browser", "status")
    usage.set_renames(renames)
    usage.record("cx", "start")
    usage.record("reflection", "start_other")
    assert usage.totals()[("reflection", "start_other")] == 2


# -- /invoke ------------------------------------------------------------------


def test_an_old_flat_name_reaches_the_new_verb_and_is_stamped(client, world):
    resp = _invoke(client, "cx_start", {"project": "awm"})
    assert resp.status_code == 200
    fn, args = world.calls[-1]
    assert fn == "start" and args["project"] == "awm" and args["_caller_pid"] == PID


def test_an_old_domain_call_reaches_the_new_verb(client, world):
    resp = _invoke(client, "cx", {"verb": "stop", "args": {"job": "j1"}})
    assert resp.status_code == 200
    fn, args = world.calls[-1]
    assert fn == "stop" and args["job"] == "j1" and args["_caller_pid"] == PID


def test_a_live_verb_is_never_shadowed(client, world):
    resp = _invoke(client, "reflection", {"verb": "compact"})
    assert resp.status_code == 200 and world.calls[-1][0] == "compact"
    resp = _invoke(client, "reflection_message_self", {})
    assert world.calls[-1][0] == "send"


def test_a_renamed_verb_with_a_hidden_target_still_runs_flat(client, world):
    assert _invoke(client, "reporting_hook_plan", {"plan": "p"}).status_code == 200
    assert world.calls[-1][0] == "hook_plan"
    resp = _invoke(client, "reporting", {"verb": "hook_plan", "args": {"plan": "p"}})
    assert resp.status_code == 200 and world.calls[-1][0] == "hook_plan"


@pytest.mark.parametrize("old,kind", [("reporting_request_human_user", "blocked"),
                                      ("reporting_request_human_review", "review")])
def test_request_human_injects_its_kind_in_both_shapes(client, world, old, kind):
    assert _invoke(client, old, {"message": "hi"}).status_code == 200
    fn, args = world.calls[-1]
    assert fn == "request_human" and args["kind"] == kind and args["message"] == "hi"
    verb = old.removeprefix("reporting_")
    # the caller cannot choose the kind the old verb implied
    resp = _invoke(client, "reporting", {"verb": verb, "args": {"message": "hi", "kind": "x"}})
    assert resp.status_code == 200
    assert world.calls[-1][1]["kind"] == kind


@pytest.mark.parametrize("old", ["scope_gather", "scope_archive_search", "kb_sweep",
                                 "project_create"])
def test_an_absent_target_is_the_ordinary_unknown_tool_error(client, world, old):
    resp = _invoke(client, old, {})
    assert resp.status_code == 404 and "Unknown tool" in resp.json()["detail"]
    assert world.calls == []


def test_an_absent_target_in_the_domain_shape_is_unknown_too(client, world):
    resp = _invoke(client, "scope", {"verb": "gather", "args": {}})
    assert resp.status_code == 404
    resp = _invoke(client, "board", {"verb": "post", "args": {}})
    assert resp.status_code == 200, "the board target is registered here"


def test_resolution_is_idempotent_and_ignores_malformed_calls():
    for name, args in (("cx_start", {}), ("x", {"verb": 3}), (7, {}), ("cx", [1])):
        once = catalog.resolve_alias(name, args)
        assert catalog.resolve_alias(*once) == once


# -- session modes and the stamp judge the call after the rewrite ---------------


@pytest.mark.parametrize("mode,old,verb_args,status", [
    ("representative", "cx_stop", {}, 403),
    ("representative", "cx_list", {}, 200),
    ("representative", "board_post", {}, 403),
    ("representative", "board_list", {}, 200),
    ("representative", "scope_post", {}, 403),
    ("representative", "scope_fetch", {}, 200),
    ("representative", "reporting_request_human_user", {}, 200),
    ("secretary", "cx_stop", {}, 200),
    ("secretary", "reflection_send", {}, 403),
    ("delegate", "board_post", {}, 403),
    ("delegate", "board_post", {"kind": "message", "reply_to": "a" * 32}, 200),
])
def test_the_mode_gate_sees_the_successor(client, world, mode, old, verb_args, status):
    world.mode["value"] = mode
    assert _invoke(client, old, verb_args).status_code == status


def test_the_old_name_in_a_domain_call_is_gated_by_its_successor(client, world):
    world.mode["value"] = "representative"
    assert _invoke(client, "cx", {"verb": "stop"}).status_code == 403
    assert _invoke(client, "cx", {"verb": "list"}).status_code == 200


# -- a peer on the previous release -------------------------------------------


def _foreign(tmp, monkeypatch, *grants):
    import awm.config.peerbook as peerbook

    path = tmp / "peers.json"
    path.write_text(json.dumps({"shaula": {"edge_url": "https://shaula:12100",
                                           "relation": "foreign", "grants": list(grants)}}))
    monkeypatch.setattr(peerbook, "peers_file", lambda: path)


def test_a_foreign_peer_reaches_a_renamed_read_by_its_old_name(client, world, tmp_path,
                                                                monkeypatch):
    _foreign(tmp_path, monkeypatch, "journals")
    headers = {"X-Awm-As": "peer:shaula"}
    resp = _invoke(client, "scope_fetch", {}, headers)
    assert resp.status_code == 200 and world.calls[-1][0] == "fetch"
    resp = _invoke(client, "scope", {"verb": "fetch", "args": {}}, headers)
    assert resp.status_code == 200


def test_a_foreign_peer_cannot_reach_a_write_through_an_alias(client, world, tmp_path,
                                                               monkeypatch):
    _foreign(tmp_path, monkeypatch, "journals")
    headers = {"X-Awm-As": "peer:shaula"}
    for old in ("scope_post", "board_post", "cx_start", "gateway_list"):
        resp = _invoke(client, old, {}, headers)
        assert resp.status_code == 404, old
    assert world.calls == []


def test_a_foreign_peer_without_the_grant_gets_the_unknown_tool_error(client, world,
                                                                       tmp_path, monkeypatch):
    _foreign(tmp_path, monkeypatch, "kb")
    resp = _invoke(client, "scope_fetch", {}, {"X-Awm-As": "peer:shaula"})
    assert resp.status_code == 404 and "Unknown tool" in resp.json()["detail"]
