"""The foreign-caller gate in ``catalog.dispatch``, the cut ``/tools`` listing, and
the rule that a foreign peer never enters this node's own catalog.

A caller stamped ``peer:<node>`` whose peer-book relation is not ``domestic`` may
run only ``read`` verbs in a category its record grants. Everything else is
refused exactly like an unknown tool. The legacy bare ``peer`` and an absent
identity are not affected.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from awm.gateway import catalog, peer_catalog
from awm.gateway.hub.registry import ServiceRecord
from awm.gateway.operations import JsonOutput, Operation

FOREIGN = "peer:hub"


class _StubRegistry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


class _FakeChannel:
    def __init__(self):
        self.calls = []
        self.grants = []

    async def call(self, fn, args, as_=None, timeout=None, grants=None):
        self.calls.append((fn, args, as_))
        self.grants.append(grants)
        return {"ran": fn}


def _fn(name, effect=None, category=None, tool=None):
    spec = {"name": name, "description": f"{name}.",
            "params": [{"name": "q", "type": "string"}]}
    if effect:
        spec["effect"] = effect
    if category:
        spec["category"] = category
    if tool:
        spec["tool"] = tool
    return spec


def _rec(name, *fns):
    return ServiceRecord(name=name, prefix=f"/svc/{name}", kind="service",
                         service_id=f"sid-{name}", api={"functions": list(fns)})


def _write_book(awm_dir, **peers):
    path = awm_dir / "state" / "peers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(peers))


def _foreign(*grants, **extra):
    return {"edge_url": "https://hub:12100", "relation": "foreign",
            "grants": list(grants), **extra}


@pytest.fixture()
def world(awm_workspace, monkeypatch):
    """kb (read/kb, read/none, write/kb), scope (read/journals, write) and a
    native op that is read/kb; peer ``hub`` is foreign holding ``kb``; ``mira`` is
    domestic."""
    awm_dir = awm_workspace["awm_dir"]
    stub = _StubRegistry([
        _rec("kb",
             _fn("search", "read", "kb"),
             _fn("stats", "read"),
             _fn("add", "write", "kb"),
             _fn("typo", "reed", "kb")),
        _rec("scope",
             _fn("archive", "read", "journals", tool="scope_archive"),
             _fn("create", "write")),
        _rec("legacy", _fn("old")),  # declares nothing: counts as write
    ])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)

    native = Operation(
        name="nat_peek", description="peek.", service_func=lambda: {"peeked": True},
        http_method="GET", http_path="/nat/peek", cli_group="nat", cli_command="peek",
        output=JsonOutput(), effect="read", category="kb")
    ops = [native]
    monkeypatch.setattr(catalog, "GATEWAY_OPERATIONS", ops)
    monkeypatch.setattr(catalog, "_GATEWAY_OPS_BY_NAME", {o.name: o for o in ops})
    monkeypatch.setattr(catalog, "_GATEWAY_MCP_TOOLS", catalog.operations_to_mcp_tools(ops))
    monkeypatch.setattr(peer_catalog, "_snapshot", {})

    _write_book(awm_dir,
                hub=_foreign("kb"),
                mira={"edge_url": "https://mira:12100", "relation": "domestic"})
    ch.awm_dir = awm_dir
    return ch


async def _refused(name, args, as_=FOREIGN):
    with pytest.raises(ValueError) as exc:
        await catalog.dispatch(name, args, as_=as_)
    return str(exc.value)


# ---------------------------------------------------------------------------
# What a foreign caller may run
# ---------------------------------------------------------------------------


async def test_read_in_a_granted_category_runs_in_the_flat_shape(world):
    assert json.loads(await catalog.dispatch("kb_search", {"q": "x"}, as_=FOREIGN)) == {
        "ran": "search"}
    assert world.calls == [("search", {"q": "x"}, FOREIGN)]


async def test_read_in_a_granted_category_runs_in_the_domain_shape(world):
    out = await catalog.dispatch(
        "kb", {"verb": "search", "args": {"q": "x"}}, as_=FOREIGN)
    assert json.loads(out) == {"ran": "search"}
    assert world.calls == [("search", {"q": "x"}, FOREIGN)]


async def test_a_native_op_is_never_open_to_a_foreign_caller(world):
    """A native handler runs in-process without the caller's identity, so even a
    native that declares read + a granted category stays closed."""
    await _refused("nat_peek", {})
    await _refused("nat", {"verb": "peek"})
    await _refused("nat", {"verb": "describe"})
    assert "nat" not in {t.name for t in catalog.list_domain_tools(
        grants=frozenset({"kb"}))}
    assert "nat_peek" not in {t.name for t in catalog.list_tools(
        grants=frozenset({"kb"}))}
    assert json.loads(await catalog.dispatch("nat_peek", {}, as_="peer:mira")) == {
        "peeked": True}, "a domestic caller still reaches it"


async def test_a_domain_call_runs_here_even_when_the_domain_is_homed_elsewhere(
        world, monkeypatch):
    monkeypatch.setenv("AWM_DOMAIN_HOME_kb", "mira")
    monkeypatch.setattr(peer_catalog, "_snapshot", {
        "mira": {"domains": {"kb": ["search"]}, "reachable": True, "error": None}})
    await catalog.dispatch("kb", {"verb": "search"}, as_=FOREIGN)
    assert world.calls, "no redirect: the foreign caller never reaches another node"


# ---------------------------------------------------------------------------
# What it may not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,args", [
    ("scope_archive", {}),                                  # read, ungranted category
    ("kb_stats", {}),                                       # read, no category
    ("kb_add", {}),                                         # write, granted category
    ("scope_create", {}),                                   # write, no category
    ("legacy_old", {}),                                     # declares nothing
    ("kb_typo", {}),                                        # unknown effect word
    ("kb", {"verb": "stats"}),
    ("kb", {"verb": "add"}),
    ("kb", {"verb": "typo"}),
    ("scope", {"verb": "archive"}),
    ("scope", {"verb": "create"}),
    ("legacy", {"verb": "old"}),
])
async def test_everything_else_is_refused(world, name, args):
    await _refused(name, args)
    assert world.calls == []


async def test_a_refusal_reads_like_an_unknown_tool(world):
    assert await _refused("kb_add", {}) == "Unknown tool: kb_add"
    assert await _refused("kb_nope", {}) == "Unknown tool: kb_nope"
    assert await _refused("scope", {"verb": "archive"}) == "Unknown tool: scope"
    assert await _refused("nope", {"verb": "x"}) == "Unknown tool: nope"
    # inside a domain it can see, a hidden verb reads like a missing one
    hidden = await _refused("kb", {"verb": "add"})
    missing = await _refused("kb", {"verb": "nope"})
    assert hidden.replace("'add'", "'nope'") == missing


async def test_providers_of_is_refused(world):
    assert await _refused("providersOf", {}) == "Unknown tool: providersOf"
    assert await _refused("providersOf", {"tool": "kb"}) == "Unknown tool: providersOf"


@pytest.mark.parametrize("name,args", [
    ("kb", {"verb": "search", "peer": "mira"}),
    ("kb", {"verb": "search", "peer": "local"}),
    ("kb", {"verb": "search", "args": {"peer": "mira"}}),
    ("kb_search", {"peer": "mira"}),
])
async def test_a_peer_argument_is_refused(world, name, args):
    await _refused(name, args)
    assert world.calls == []


async def test_an_empty_peer_argument_is_not_a_hop(world):
    await catalog.dispatch("kb", {"verb": "search", "peer": None}, as_=FOREIGN)
    assert len(world.calls) == 1


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


async def test_describe_shows_only_the_allowed_verbs(world):
    out = json.loads(await catalog.dispatch("kb", {"verb": "describe"}, as_=FOREIGN))
    assert [v["verb"] for v in out["verbs"]] == ["search"]


async def test_describe_of_a_single_hidden_verb_is_refused(world):
    await _refused("kb", {"verb": "describe", "args": {"verb": "add"}})


async def test_describe_of_a_domain_with_no_allowed_verb_is_refused(world):
    assert await _refused("scope", {"verb": "describe"}) == "Unknown tool: scope"


# ---------------------------------------------------------------------------
# Grants are read on every call
# ---------------------------------------------------------------------------


async def test_a_revoked_grant_bites_on_the_next_call(world):
    await catalog.dispatch("kb_search", {}, as_=FOREIGN)
    _write_book(world.awm_dir, hub=_foreign())
    await _refused("kb_search", {})
    await _refused("kb", {"verb": "search"})


async def test_a_new_grant_bites_on_the_next_call(world):
    await _refused("scope_archive", {})
    _write_book(world.awm_dir, hub=_foreign("kb", "journals"))
    await catalog.dispatch("scope_archive", {}, as_=FOREIGN)
    assert world.calls[-1][0] == "archive"


async def test_a_node_that_is_not_in_the_book_is_foreign_with_no_grants(world):
    await _refused("kb_search", {}, as_="peer:stranger")
    await _refused("kb", {"verb": "describe"}, as_="peer:stranger")


async def test_a_peer_flipped_to_foreign_is_gated_on_the_next_call(world):
    await catalog.dispatch("kb_add", {}, as_="peer:mira")
    _write_book(world.awm_dir, mira=_foreign())
    await _refused("kb_add", {}, as_="peer:mira")


# ---------------------------------------------------------------------------
# Callers the gate leaves alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("as_", [None, "peer", "peer:mira", "placed-1"])
async def test_other_callers_are_unchanged(world, as_):
    await catalog.dispatch("kb_add", {}, as_=as_)
    await catalog.dispatch("kb", {"verb": "add"}, as_=as_)
    assert [c[0] for c in world.calls] == ["add", "add"]


async def test_dispatch_runs_the_entry_the_gate_approved(world, monkeypatch):
    """The catalog entry carries its function; a different registry at call time
    cannot redirect the call."""
    other = _FakeChannel()
    approved = catalog._domain_catalog()
    stub = _StubRegistry([_rec("kb", _fn("search", "write"))])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: other)
    cut = catalog._foreign_catalog(approved, frozenset({"kb"}))
    out = await catalog._dispatch_domain(
        "kb", {"verb": "search"}, FOREIGN, cut, local_only=True)
    assert json.loads(out) == {"ran": "search"}
    assert other.calls[0][0] == "search"
    entry = cut["kb"][0]
    assert entry["rec"].name == "kb" and entry["fn"] == "search" and entry["op"] is None


# ---------------------------------------------------------------------------
# HTTP level
# ---------------------------------------------------------------------------


@pytest.fixture()
def http(world):
    from fastapi.testclient import TestClient

    from awm.gateway.server import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _post(http, name, args, as_=FOREIGN):
    return http.post("/invoke", json={"name": name, "args": args},
                     headers={"X-Awm-As": as_} if as_ else {})


def test_a_refusal_is_a_404_over_http_and_an_allowed_read_is_a_200(http, world):
    ok = _post(http, "kb_search", {"q": "x"})
    assert ok.status_code == 200 and json.loads(ok.json()["result"]) == {"ran": "search"}
    for name, args in (("kb_add", {}), ("scope_archive", {}), ("providersOf", {}),
                       ("kb", {"verb": "add"}), ("kb_search", {"peer": "mira"}),
                       ("nat_peek", {}), ("nope", {})):
        resp = _post(http, name, args)
        assert resp.status_code == 404, (name, args, resp.text)
        assert "Unknown" in resp.json()["detail"]
    # a body whose args is not an object is a refusal as well
    assert _post(http, "kb_search", [1]).status_code == 404
    assert _post(http, "kb", {"verb": ["x"]}).status_code == 404


def test_a_revoked_grant_is_a_404_over_http_on_the_next_request(http, world):
    assert _post(http, "kb_search", {}).status_code == 200
    _write_book(world.awm_dir, hub=_foreign())
    assert _post(http, "kb_search", {}).status_code == 404


def test_other_callers_get_200_over_http(http, world):
    for as_ in (None, "peer", "peer:mira"):
        assert _post(http, "kb_add", {}, as_=as_).status_code == 200


def test_tools_over_http_is_cut_for_a_foreign_caller(http, world):
    names = {t["name"] for t in http.get(
        "/tools", params={"view": "domains"}, headers={"X-Awm-As": FOREIGN}
    ).json()["tools"]}
    assert names == {"kb"}


# ---------------------------------------------------------------------------
# The /svc backstop
# ---------------------------------------------------------------------------


class _Req:
    url = SimpleNamespace(path="/svc/kb/fn/search")

    async def body(self):
        return b""


async def test_the_svc_doors_refuse_a_foreign_caller(world, monkeypatch):
    from awm.gateway.hub import proxy

    monkeypatch.setattr(proxy.rpc, "get_control", lambda sid: None)

    for call in (proxy.proxy_service_http(_Req(), "sid-kb", as_=FOREIGN),
                 proxy.open_session_via_http(_Req(), "sid-kb", as_=FOREIGN),
                 proxy.proxy_service_http(_Req(), "sid-kb", as_="peer:"),
                 proxy.proxy_service_http(_Req(), "sid-kb", as_="peer:stranger")):
        resp = await call
        assert resp.status_code == 404
    # domestic and local callers get past the backstop (no control channel here)
    for as_ in (None, "peer", "peer:mira"):
        resp = await proxy.proxy_service_http(_Req(), "sid-none", as_=as_)
        assert resp.status_code == 503


async def test_the_svc_sockets_close_a_foreign_caller_before_accepting(world):
    from awm.gateway.hub import proxy

    class WS:
        def __init__(self):
            self.closed, self.accepted = None, False

        async def close(self, code=1000, reason=""):
            self.closed = code

        async def accept(self):
            self.accepted = True

    for call in (lambda ws: proxy.proxy_service_emit_ws(ws, "sid-kb", "t", as_=FOREIGN),
                 lambda ws: proxy.proxy_session_ws(ws, "sid-kb", "abc", as_=FOREIGN)):
        ws = WS()
        await call(ws)
        assert ws.closed == 1008 and not ws.accepted


async def test_one_peer_that_never_answers_cannot_hold_the_sweep(world, monkeypatch):
    import asyncio

    async def hang(entry):
        await asyncio.sleep(60)

    monkeypatch.setattr(peer_catalog, "_fetch_peer", hang)
    monkeypatch.setattr(peer_catalog, "_PEER_FETCH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(peer_catalog, "_sweep_lock", None)
    monkeypatch.setattr(peer_catalog, "_swept_at", 0.0)
    snap = await asyncio.wait_for(peer_catalog.sweep(), 5)
    assert snap["mira"]["reachable"] is False
    assert "TimeoutError" in snap["mira"]["error"]


# ---------------------------------------------------------------------------
# A granted verb that refuses part of what it serves
# ---------------------------------------------------------------------------


def _raising(world, message, error_class):
    exc = catalog.rpc.RpcError(message)
    exc.error_class = error_class

    async def call(fn, args, as_=None, timeout=None, grants=None):
        raise exc

    world.call = call
    return exc


async def test_a_service_permission_error_reads_like_an_unknown_tool_to_a_foreign_caller(world):
    _raising(world, "peers may read journal posts only", "PermissionError")
    assert await _refused("kb_search", {}) == "Unknown tool: kb_search"
    refused = await _refused("kb", {"verb": "search"})
    assert refused == (await _refused("kb", {"verb": "add"})).replace("'add'", "'search'")
    assert "journal" not in refused


async def test_other_service_errors_reach_a_foreign_caller_unchanged(world):
    for error_class in ("ValueError", "KeyError", None):
        exc = _raising(world, "bad arguments", error_class)
        with pytest.raises(catalog.rpc.RpcError) as got:
            await catalog.dispatch("kb_search", {}, as_=FOREIGN)
        assert got.value is exc


@pytest.mark.parametrize("as_", [None, "peer", "peer:mira"])
async def test_a_permission_error_for_other_callers_is_not_remapped(world, as_):
    exc = _raising(world, "refused", "PermissionError")
    with pytest.raises(catalog.rpc.RpcError) as got:
        await catalog.dispatch("kb_search", {}, as_=as_)
    assert got.value is exc


def test_a_service_permission_error_is_a_404_over_http_for_a_foreign_caller(http, world):
    _raising(world, "goals are not readable by peers", "PermissionError")
    for name, args in (("kb_search", {}), ("kb", {"verb": "search"})):
        resp = _post(http, name, args)
        assert resp.status_code == 404, resp.text
        assert "goals" not in resp.text
    assert _post(http, "kb_search", {}, as_="peer:mira").status_code == 500
