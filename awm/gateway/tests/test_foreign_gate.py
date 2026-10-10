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

    async def call(self, fn, args, as_=None, timeout=None):
        self.calls.append((fn, args, as_))
        return {"ran": fn}


def _fn(name, effect=None, category=None, tool=None):
    spec = {"name": name, "description": f"{name}.", "params": []}
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
    ("more", {"domain": "kb", "verb": "search", "peer": "mira"}),
])
async def test_a_peer_argument_is_refused(world, name, args):
    await _refused(name, args)
    assert world.calls == []


async def test_an_empty_peer_argument_is_not_a_hop(world):
    await catalog.dispatch("kb", {"verb": "search", "peer": None}, as_=FOREIGN)
    assert len(world.calls) == 1


# ---------------------------------------------------------------------------
# describe and more
# ---------------------------------------------------------------------------


async def test_describe_shows_only_the_allowed_verbs(world):
    out = json.loads(await catalog.dispatch("kb", {"verb": "describe"}, as_=FOREIGN))
    assert [v["verb"] for v in out["verbs"]] == ["search"]


async def test_describe_of_a_single_hidden_verb_is_refused(world):
    await _refused("kb", {"verb": "describe", "args": {"verb": "add"}})


async def test_describe_of_a_domain_with_no_allowed_verb_is_refused(world):
    assert await _refused("scope", {"verb": "describe"}) == "Unknown tool: scope"


async def test_more_lists_only_allowed_verbs(world):
    out = await catalog.dispatch("more", {}, as_=FOREIGN)
    assert out == "- kb: verbs: search"
    assert "scope" not in out and "add" not in out


async def test_more_calls_only_allowed_verbs(world):
    out = await catalog.dispatch(
        "more", {"domain": "kb", "verb": "search", "args": {"q": 1}}, as_=FOREIGN)
    assert json.loads(out) == {"ran": "search"}
    await _refused("more", {"domain": "kb", "verb": "add"})
    await _refused("more", {"domain": "scope", "verb": "archive"})
    await _refused("more", {"domain": "providersOf"})
    await _refused("more", {"domain": "kb@hub", "verb": "search"})


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


async def test_the_legacy_bearer_still_reaches_providers_of(world):
    out = json.loads(await catalog.dispatch("providersOf", {}, as_="peer"))
    assert "tools" in out


# ---------------------------------------------------------------------------
# /tools
# ---------------------------------------------------------------------------


def test_a_foreign_domain_listing_is_cut_to_what_it_may_call(world):
    tools = {t.name: t for t in catalog.list_domain_tools(
        peers=True, tiers=True, grants=frozenset({"kb"}))}
    assert set(tools) == {"kb"}, "no providersOf, no more, no scope, no native"
    assert tools["kb"].inputSchema["properties"]["verb"]["enum"] == ["search", "describe"]
    assert "peer" not in tools["kb"].inputSchema["properties"]
    assert "add" not in tools["kb"].description


def test_a_domestic_listing_is_unchanged(world):
    names = {t.name for t in catalog.list_domain_tools()}
    assert {"kb", "scope", "legacy", "nat"} <= names
    kb = next(t for t in catalog.list_domain_tools() if t.name == "kb")
    assert "peer" in kb.inputSchema["properties"]


def test_a_foreign_flat_listing_is_cut_to_what_it_may_call(world):
    assert {t.name for t in catalog.list_tools(grants=frozenset({"kb"}))} == {
        "kb_search"}
    assert {t.name for t in catalog.list_tools(grants=frozenset({"kb", "journals"}))} == {
        "kb_search", "scope_archive"}
    assert {"kb_add", "legacy_old"} <= {t.name for t in catalog.list_tools()}


def test_the_tools_endpoint_reads_the_edge_stamp(world):
    from awm.gateway import server

    def listing(as_, **kw):
        headers = {"X-Awm-As": as_} if as_ else {}
        out = server.list_tools_endpoint(SimpleNamespace(headers=headers), **kw)
        return {t["name"] for t in out["tools"]}

    assert listing(FOREIGN, view="domains") == {"kb"}
    assert listing(FOREIGN) == {"kb_search"}
    assert listing(FOREIGN, view="domains", peers=1, tiers=1) == {"kb"}
    assert listing("peer:") == set() and listing("peer: ") == set()
    assert "scope" in listing(None, view="domains")
    assert "kb_add" in listing("peer")


# ---------------------------------------------------------------------------
# A foreign peer never enters our catalog
# ---------------------------------------------------------------------------


def _hub_snapshot():
    return {
        "mira": {"domains": {"orch": ["run"]}, "reachable": True, "error": None},
        "hub": {"domains": {"hubonly": ["go"], "kb": ["search"]},
                "reachable": True, "error": None},
    }


def test_a_foreign_peers_domain_never_appears_in_the_fleet(world, monkeypatch):
    monkeypatch.setattr(peer_catalog, "_snapshot", _hub_snapshot())
    assert set(peer_catalog.snapshot()) == {"mira"}
    assert "hubonly" not in peer_catalog.fleet_domains({})
    assert "orch" in peer_catalog.fleet_domains({})
    assert peer_catalog.resolve("hubonly", {})["reason"] == peer_catalog.REASON_UNKNOWN
    with pytest.raises(ValueError):
        peer_catalog.choose_target("hubonly", "hub", {})


def test_a_snapshot_passed_in_is_filtered_too(world):
    assert "hubonly" not in peer_catalog.fleet_domains({}, _hub_snapshot())
    res = peer_catalog.resolve("kb", {}, _hub_snapshot())
    assert res["reason"] == peer_catalog.REASON_UNKNOWN


def test_a_foreign_peers_domain_is_in_no_description_and_not_in_more(
        world, monkeypatch):
    monkeypatch.setattr(peer_catalog, "_snapshot", _hub_snapshot())
    text = " ".join(t.description for t in catalog.list_domain_tools(
        peers=True, tiers=True))
    assert "hubonly" not in text
    assert "orch" in text, "a domestic peer's domain is still indexed"
    assert "hubonly" not in catalog._more_index({})
    assert "hubonly" not in " ".join(
        t.description for t in catalog.list_domain_tools(peers=True))


async def test_a_foreign_peer_is_never_a_default_provider(world, monkeypatch):
    monkeypatch.setattr(peer_catalog, "_snapshot", _hub_snapshot())
    res = peer_catalog.resolve("kb", catalog._local_domain_verbs(catalog._domain_catalog()))
    assert [p["peer"] for p in res["providers"]] == ["local"]
    with pytest.raises(ValueError, match="Unknown tool"):
        await catalog.dispatch("hubonly", {"verb": "go"})


def test_a_declared_home_owned_by_a_foreign_peer_is_ignored(world, monkeypatch):
    monkeypatch.setenv("AWM_DOMAIN_HOME_vault", "hub")
    assert "vault" not in peer_catalog.declared_homes()
    monkeypatch.setenv("AWM_DOMAIN_HOME_vault", "mira")
    assert peer_catalog.declared_homes()["vault"] == "mira"


async def test_the_sweep_skips_foreign_peers(world, monkeypatch):
    fetched = []

    async def fake_fetch(entry):
        fetched.append(entry["name"])
        return {"orch": ["run"]}

    monkeypatch.setattr(peer_catalog, "_fetch_peer", fake_fetch)
    monkeypatch.setattr(peer_catalog, "_sweep_lock", None)
    monkeypatch.setattr(peer_catalog, "_swept_at", 0.0)
    snap = await peer_catalog.sweep()
    assert fetched == ["mira"]
    assert set(snap) == {"mira"}


# ---------------------------------------------------------------------------
# The MCP proxies dial a peer edge through gatewayclient's token path
# ---------------------------------------------------------------------------


def _fake_response(status=200, body=None):
    return SimpleNamespace(
        status_code=status, text=json.dumps(body or {}), json=lambda: body or {},
        raise_for_status=lambda: None)


def test_the_stdio_proxy_dials_a_redirect_through_peer_send_sync(monkeypatch):
    from awm import gatewayclient
    from awm.gateway import mcp_stdio

    seen = {}

    def fake_send(peer, entry, send):
        seen["peer"], seen["entry"] = peer, entry
        return _fake_response(200, {"result": "R"})

    monkeypatch.setattr(gatewayclient, "peer_send_sync", fake_send)
    monkeypatch.setattr(gatewayclient, "_peer_ca", lambda: "/x/ca.pem")
    entry = {"peer": "mira", "edge_url": "https://mira:12100", "relation": "domestic"}
    assert mcp_stdio._peer_invoke("mira", "kb", {"verb": "get"}, None, entry=entry) == {
        "result": "R"}
    assert seen == {"peer": "mira", "entry": entry}


def test_the_stdio_proxy_raises_the_peers_refusal(monkeypatch):
    from awm import gatewayclient
    from awm.gateway import mcp_stdio

    monkeypatch.setattr(gatewayclient, "peer_send_sync",
                        lambda p, e, s: _fake_response(404, {"detail": "no"}))
    monkeypatch.setattr(gatewayclient, "_peer_ca", lambda: "/x/ca.pem")
    with pytest.raises(mcp_stdio._HTTPStatusError) as exc:
        mcp_stdio._peer_invoke("mira", "kb", {}, None,
                               entry={"edge_url": "https://mira:12100"})
    assert exc.value.status == 404


def test_the_sdk_proxy_dials_a_redirect_through_peer_send(monkeypatch):
    import asyncio

    sdk = pytest.importorskip("awm.gateway.mcp_server_sdk")
    from awm import gatewayclient

    seen = {}

    async def fake_send(peer, entry, send):
        seen["peer"], seen["entry"] = peer, entry
        return _fake_response(200, {"result": "R"})

    monkeypatch.setattr(gatewayclient, "peer_send", fake_send)
    monkeypatch.setattr(gatewayclient, "_peer_ca", lambda: "/x/ca.pem")
    entry = {"peer": "mira", "edge_url": "https://mira:12100", "relation": "domestic"}
    out = asyncio.run(sdk._peer_invoke("mira", "kb", {"verb": "get"}, None, entry=entry))
    assert out == {"result": "R"}
    assert seen == {"peer": "mira", "entry": entry}


def test_a_node_suffixed_tool_name_goes_to_the_local_gateway_not_a_peer(monkeypatch):
    from awm.gateway import mcp_stdio

    sent, dialled = [], []
    monkeypatch.setattr(mcp_stdio, "_peer_invoke",
                        lambda *a, **k: dialled.append(a) or {"result": "x"})
    monkeypatch.setattr(mcp_stdio, "_request_with_retry",
                        lambda method, path, json_body=None, **kw: sent.append(json_body)
                        or {"result": "refused"})
    mcp_stdio._handle_tools_call({"name": "kb@mira", "arguments": {"verb": "get"}})
    assert not dialled
    assert sent[0]["name"] == "kb@mira"


def test_a_peer_redirect_payload_names_the_relation(awm_workspace):
    _write_book(awm_workspace["awm_dir"], mira={
        "edge_url": "https://mira:12100", "relation": "domestic"})
    assert peer_catalog.PeerRedirect("mira", "kb", "get").payload()["relation"] == "domestic"


# ---------------------------------------------------------------------------
# Hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stamp", ["peer:", "peer: ", "peer:   "])
async def test_a_malformed_peer_stamp_is_foreign_with_no_grants(world, stamp):
    assert catalog.foreign_grants(stamp) == frozenset()
    await _refused("kb_search", {}, as_=stamp)
    await _refused("kb", {"verb": "search"}, as_=stamp)
    await _refused("kb", {"verb": "describe"}, as_=stamp)
    assert world.calls == []


def test_only_a_real_node_stamp_or_no_stamp_is_not_foreign(world):
    assert catalog.foreign_grants(None) is None
    assert catalog.foreign_grants("peer") is None
    assert catalog.foreign_grants("placed-1") is None
    assert catalog.foreign_grants("peer:mira") is None
    assert catalog.foreign_grants("peer:hub") == frozenset({"kb"})


@pytest.mark.parametrize("book", ["{ not json", "[]", "null", None])
async def test_a_corrupt_or_missing_book_fails_closed(world, book):
    path = world.awm_dir / "state" / "peers.json"
    if book is None:
        path.unlink()
    else:
        path.write_text(book)
    await _refused("kb_search", {})
    await _refused("kb", {"verb": "search"})
    await _refused("kb_search", {}, as_="peer:mira")   # nobody is known to be domestic
    assert world.calls == []
    assert catalog.foreign_grants("peer") is None, "the legacy bearer is not a node stamp"


def test_a_corrupt_book_leaves_no_provider(world, monkeypatch):
    monkeypatch.setattr(peer_catalog, "_snapshot", _hub_snapshot())
    (world.awm_dir / "state" / "peers.json").write_text("{ not json")
    assert peer_catalog.snapshot() == {}
    assert "orch" not in peer_catalog.fleet_domains({})


def test_a_peer_missing_from_the_book_is_not_a_provider(world, monkeypatch):
    monkeypatch.setattr(peer_catalog, "_snapshot", {
        "ghost": {"domains": {"spooky": ["boo"]}, "reachable": True, "error": None}})
    assert "spooky" not in peer_catalog.fleet_domains({})


async def test_a_malformed_manifest_entry_cannot_break_the_foreign_surface(
        world, monkeypatch):
    stub = _StubRegistry([
        _rec("kb", _fn("search", "read", "kb")),
        _rec("bad",
             {"name": "a", "effect": "read", "category": ["kb"]},
             {"name": "b", "effect": ["read"], "category": "kb"},
             {"name": "c", "effect": "read", "category": {"x": 1}},
             {"name": "d", "effect": 7, "category": 7}),
    ])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    for verb in "abcd":
        await _refused(f"bad_{verb}", {})
        await _refused("bad", {"verb": verb})
    assert {t.name for t in catalog.list_tools(grants=frozenset({"kb"}))} == {"kb_search"}
    assert {t.name for t in catalog.list_domain_tools(grants=frozenset({"kb"}))} == {"kb"}
    assert catalog._allowed("read", ["kb"], frozenset({"kb"})) is False
    assert catalog._allowed(["read"], "kb", frozenset({"kb"})) is False


@pytest.mark.parametrize("name,args", [
    ("kb_search", [1, 2]),
    ("kb_search", "text"),
    ("kb", {"verb": ["search"]}),
    ("kb", {"verb": {"a": 1}}),
    ("kb", {"verb": "search", "args": [1]}),
    ("kb", {"verb": "describe", "args": "x"}),
    ("more", {"domain": ["kb"]}),
    (["kb_search"], {}),
])
async def test_malformed_input_from_a_foreign_caller_is_a_refusal_not_a_crash(
        world, name, args):
    with pytest.raises(ValueError):
        await catalog.dispatch(name, args, as_=FOREIGN)
    assert world.calls == []


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
                       ("more", {"domain": "kb", "verb": "add"}),
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

    async def call(fn, args, as_=None, timeout=None):
        raise exc

    world.call = call
    return exc


async def test_a_service_permission_error_reads_like_an_unknown_tool_to_a_foreign_caller(world):
    _raising(world, "peers may read journal posts only", "PermissionError")
    assert await _refused("kb_search", {}) == "Unknown tool: kb_search"
    refused = await _refused("kb", {"verb": "search"})
    assert refused == (await _refused("kb", {"verb": "add"})).replace("'add'", "'search'")
    assert "journal" not in refused


async def test_the_same_refusal_through_more_is_an_unknown_tool(world):
    _raising(world, "peers may read journal posts only", "PermissionError")
    with pytest.raises(ValueError, match="Unknown"):
        await catalog.dispatch("more", {"domain": "kb", "verb": "search"}, as_=FOREIGN)


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
