"""Core and discoverable tiers: the ``tier`` key, the narrowed MCP surface, and
the ``more`` call-through.

The tier lives in each service folder's ``service.toml`` and nowhere else. The
filter sits in ``catalog.list_domain_tools`` behind ``tiers=True``; everything
that dispatches, describes or sweeps reads the unfiltered catalog, so a
discoverable domain is hidden from the tool list but never uncallable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from awm.gateway import catalog, mcp_more, mcp_stdio, peer_catalog
from awm.gateway.hub import discovery
from awm.gateway.hub.registry import ServiceRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


def _rec(name, tools):
    return ServiceRecord(
        name=name, prefix=f"/svc/{name}", kind="service", service_id=f"sid-{name}",
        api={"functions": [
            {"name": t.partition("_")[2] or t, "tool": t,
             "description": f"{t}.", "params": []} for t in tools]},
    )


@pytest.fixture()
def tiered(tmp_path, monkeypatch, awm_workspace):
    """Local services ``scopes`` (core), ``kb`` (no service.toml), ``hpcllm``
    (tier-less toml) and ``broken`` (unparseable toml); mira alone runs ``orch``."""
    root = tmp_path / "services"
    for name, toml in (("scopes", 'tier = "core"\n'), ("kb", None),
                       ("hpcllm", '# nothing to say\n'),
                       ("broken", "tier = [not toml")):
        (root / name).mkdir(parents=True)
        (root / name / "run.sh").write_text("#!/bin/bash\n")
        if toml is not None:
            (root / name / "service.toml").write_text(toml)
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    monkeypatch.delenv("AWM_TWOFA_PEER", raising=False)

    stub = _StubRegistry([
        _rec("scopes", ["scope_search"]),
        _rec("kb", ["kb_get"]),
        _rec("hpcllm", ["hpcllm_ask"]),
        _rec("broken", ["broken_run"]),
    ])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    snap = {"mira": {"domains": {"orch": ["run"], "scope": ["search"]},
                     "reachable": True, "error": None}}
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    return root


def _names(tools):
    return [t.name for t in tools]


# ---------------------------------------------------------------------------
# Reading the tier
# ---------------------------------------------------------------------------


def test_tier_reads_core_and_defaults_to_discoverable(tiered):
    assert discovery.service_tier("scopes") == discovery.TIER_CORE
    assert discovery.service_tier("kb") == discovery.TIER_DISCOVERABLE
    assert discovery.service_tier("hpcllm") == discovery.TIER_DISCOVERABLE
    assert discovery.service_tier("not-a-folder") == discovery.TIER_DISCOVERABLE


def test_malformed_service_toml_reads_as_discoverable(tiered):
    assert discovery.service_tier("broken") == discovery.TIER_DISCOVERABLE


@pytest.mark.parametrize("body", [
    'tier = "discovered"\n', 'tier = 1\n', 'tier = ["core"]\n', 'tier = ""\n'])
def test_unknown_tier_values_are_discoverable(tmp_path, body):
    (tmp_path / "service.toml").write_text(body)
    assert discovery.read_tier(tmp_path) == discovery.TIER_DISCOVERABLE


def test_tier_is_reread_when_the_file_changes(tmp_path):
    toml = tmp_path / "service.toml"
    toml.write_text("tier = \"core\"\n")
    assert discovery.read_tier(tmp_path) == discovery.TIER_CORE
    toml.write_text("# gone\n")
    assert discovery.read_tier(tmp_path) == discovery.TIER_DISCOVERABLE


def test_a_path_like_service_name_is_never_core(tiered):
    assert discovery.service_tier("../scopes") == discovery.TIER_DISCOVERABLE


# ---------------------------------------------------------------------------
# The profile gate must not read a tier-only file as gated off
# ---------------------------------------------------------------------------


def _folder(root, name, toml):
    d = root / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/bash\n")
    if toml is not None:
        (d / "service.toml").write_text(toml)


@pytest.fixture()
def gate_root(tmp_path, monkeypatch, awm_workspace):
    root = tmp_path / "gate-services"
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    return root


def test_tier_only_service_toml_keeps_the_service_enabled(gate_root):
    _folder(gate_root, "tier-only", 'tier = "core"\n')
    assert discovery._read_profiles(gate_root / "tier-only") is None
    assert discovery.is_enabled("tier-only") is True
    spec = next(s for s in discovery.discover_services() if s.name == "tier-only")
    assert spec.enabled is True


def test_service_with_no_service_toml_still_starts(gate_root):
    _folder(gate_root, "bare", None)
    assert discovery.is_enabled("bare") is True
    assert next(s for s in discovery.discover_services()
                if s.name == "bare").enabled is True


def test_tier_does_not_lift_a_profile_gate(gate_root, monkeypatch):
    _folder(gate_root, "gated-core", 'profiles = ["gamebot"]\ntier = "core"\n')
    assert discovery.is_enabled("gated-core") is False
    monkeypatch.setenv("AWM_PROFILES", "gamebot")
    assert discovery.is_enabled("gated-core") is True


def test_profiles_of_the_wrong_type_still_gate_off(gate_root):
    _folder(gate_root, "odd", 'profiles = "gamebot"\n')
    assert discovery.is_enabled("odd") is False


def test_unparseable_service_toml_is_still_gated_off(gate_root):
    _folder(gate_root, "corrupt", "tier = [not toml")
    assert discovery.is_enabled("corrupt") is False


# ---------------------------------------------------------------------------
# The shipped service.toml files
# ---------------------------------------------------------------------------

_SHIPPED_CORE = {
    "scopes", "reflection", "social", "ssh", "dvc", "precedence", "tether",
    "graphify", "rlm-browser", "cx",
}


def test_shipped_services_declare_the_expected_core_set():
    root = Path(__file__).resolve().parents[2] / "services"
    folders = [d for d in root.iterdir() if (d / "run.sh").is_file()]
    core = {d.name for d in folders
            if discovery.read_tier(d) == discovery.TIER_CORE}
    # `board` arrives with its own service.toml from another branch.
    assert core - {"board"} == _SHIPPED_CORE
    assert "agents" not in core and "hpcllm" not in core


def test_shipped_profile_gates_survive_the_tier_key():
    root = Path(__file__).resolve().parents[2] / "services"
    assert discovery._read_profiles(root / "rlm-browser") == ["gamebot"]
    assert discovery._read_profiles(root / "cx") == ["cx"]
    assert discovery._read_profiles(root / "scopes") is None


# ---------------------------------------------------------------------------
# The narrowed surface
# ---------------------------------------------------------------------------


def test_core_domain_is_listed_and_discoverable_ones_are_hidden(tiered):
    names = _names(catalog.list_domain_tools(peers=True, tiers=True))
    assert "scope" in names
    assert {"services", "peer"} <= set(names)
    assert "providersOf" in names and "more" in names
    for hidden in ("kb", "hpcllm", "broken", "gateway", "config"):
        assert hidden not in names
    assert len(names) == len(set(names))


def test_a_peer_only_domain_is_hidden_and_indexed_with_its_verbs(tiered):
    tools = {t.name: t for t in catalog.list_domain_tools(peers=True, tiers=True)}
    assert "orch" not in tools
    assert "- orch: verbs: run [mira]" in tools["more"].description


def test_the_index_names_each_hidden_domain_once_and_no_core_domain(tiered):
    desc = {t.name: t for t in catalog.list_domain_tools(
        peers=True, tiers=True)}["more"].description
    lines = [ln for ln in desc.splitlines() if ln.startswith("- ")]
    assert {ln.split(":")[0] for ln in lines} == {
        "- kb", "- hpcllm", "- broken", "- orch", "- gateway", "- config"}
    assert "- scope:" not in desc and "- services:" not in desc
    assert "[this node]" in next(ln for ln in lines if ln.startswith("- kb:"))


def test_a_blurb_replaces_the_verb_list_in_the_index(tiered, monkeypatch):
    rec = _rec("kb", ["kb_get"])
    rec.api["description"] = "Knowledge base. Search it before asking."
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([rec]))
    desc = {t.name: t for t in catalog.list_domain_tools(
        peers=True, tiers=True)}["more"].description
    assert "- kb: Knowledge base. [this node]" in desc


def test_a_peer_cannot_make_a_domain_core(tiered, monkeypatch):
    """The snapshot carries verbs only; a domain is core on our say-so alone."""
    snap = {"mira": {"domains": {"kb": ["get"], "tether": ["send"]},
                     "reachable": True, "error": None}}
    monkeypatch.setattr(peer_catalog, "snapshot", lambda: snap)
    names = _names(catalog.list_domain_tools(peers=True, tiers=True))
    assert "tether" not in names and "kb" not in names


def test_peers_false_is_unchanged_by_the_tier_work(tiered):
    plain = catalog.list_domain_tools()
    assert _names(catalog.list_domain_tools(tiers=True)) == _names(plain)
    assert {"kb", "hpcllm", "scope", "gateway"} <= set(_names(plain))
    assert "more" not in _names(plain) and "providersOf" not in _names(plain)


def test_the_fleet_view_without_tiers_is_unchanged(tiered):
    names = _names(catalog.list_domain_tools(peers=True))
    assert {"kb", "hpcllm", "orch", "scope"} <= set(names)
    assert "more" not in names


def test_the_more_tool_is_a_read_router_with_optional_arguments(tiered):
    more = {t.name: t for t in catalog.list_domain_tools(
        peers=True, tiers=True)}["more"]
    assert more.meta == {"effect": "read"}
    assert set(more.inputSchema["properties"]) == {"domain", "verb", "args", "peer"}
    assert not more.inputSchema.get("required")


# ---------------------------------------------------------------------------
# The call-through
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self):
        self.calls = []

    async def call(self, fn, args, as_=None, timeout=None):
        self.calls.append((fn, args, as_))
        return {"ok": fn, "args": args}


def test_rewrite_maps_more_to_the_direct_call():
    assert mcp_more.rewrite_call("more", {
        "domain": "kb", "verb": "get", "args": {"id": 1}, "peer": "mira"}) == (
        "kb", {"verb": "get", "args": {"id": 1}, "peer": "mira"})
    assert mcp_more.rewrite_call("more", {"domain": "kb", "verb": "get"}) == (
        "kb", {"verb": "get"})
    assert mcp_more.rewrite_call("scope", {"verb": "x"}) == ("scope", {"verb": "x"})


def test_rewrite_leaves_a_domainless_call_for_the_gateway_to_list():
    for args in ({}, {"verb": "list"}, {"domain": ""}, {"domain": 7}):
        assert mcp_more.rewrite_call("more", args) == ("more", args)


async def test_a_discoverable_domain_gives_the_same_result_through_more(
        tiered, monkeypatch):
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    direct = await catalog.dispatch(
        "kb", {"verb": "get", "args": {"id": 3}}, as_="placed-1")
    via_more = await catalog.dispatch(*mcp_more.rewrite_call(
        "more", {"domain": "kb", "verb": "get", "args": {"id": 3}}),
        as_="placed-1")
    assert via_more == direct
    assert ch.calls[0] == ch.calls[1] == ("get", {"id": 3}, "placed-1")


async def test_describe_through_more_matches_a_direct_describe(tiered):
    direct = await catalog.dispatch("kb", {"verb": "describe"})
    name, args = mcp_more.rewrite_call(
        "more", {"domain": "kb", "verb": "describe"})
    assert await catalog.dispatch(name, args) == direct
    assert json.loads(direct)["domain"] == "kb"


async def test_an_unknown_domain_errors_the_same_through_more(tiered):
    with pytest.raises(ValueError) as direct:
        await catalog.dispatch("nope", {"verb": "x"})
    with pytest.raises(ValueError) as via_more:
        await catalog.dispatch(*mcp_more.rewrite_call(
            "more", {"domain": "nope", "verb": "x"}))
    assert str(via_more.value) == str(direct.value)


async def test_a_peer_only_domain_redirects_through_more_like_a_direct_call(
        tiered, monkeypatch):
    monkeypatch.setattr("awm.gateway.peers.resolve", lambda n: {"name": n})
    with pytest.raises(peer_catalog.PeerRedirect) as direct:
        await catalog.dispatch("orch", {"verb": "run"})
    with pytest.raises(peer_catalog.PeerRedirect) as via_more:
        await catalog.dispatch(*mcp_more.rewrite_call(
            "more", {"domain": "orch", "verb": "run"}))
    assert (via_more.value.peer, via_more.value.tool) == (
        direct.value.peer, direct.value.tool) == ("mira", "orch")


async def test_more_with_no_domain_lists_the_discoverable_domains(tiered):
    out = await catalog.dispatch("more", {})
    assert out == await catalog.dispatch("more", {"verb": "list"})
    lines = out.splitlines()
    assert all(ln.startswith("- ") for ln in lines)
    assert any(ln.startswith("- kb:") for ln in lines)
    assert any(ln.startswith("- orch:") and ln.endswith("[mira]") for ln in lines)
    assert not any(ln.startswith("- scope:") for ln in lines)


async def test_more_reaching_the_gateway_with_a_domain_is_refused_not_run(tiered):
    """Only the proxy rewrite stamps the caller; an unrewritten call must not
    slip a domain verb past the stamp and the gates."""
    with pytest.raises(ValueError, match="rewritten by the MCP proxy"):
        await catalog.dispatch("more", {"domain": "kb", "verb": "get"})


# ---------------------------------------------------------------------------
# Both MCP proxies: the rewrite carries the same headers and body
# ---------------------------------------------------------------------------


def _calls_via_stdio(monkeypatch, tool_calls):
    sent = []

    def fake(method, path, json_body=None, headers=None, **kw):
        sent.append((method, path, json_body, dict(headers or {})))
        return {"result": "OK"}

    monkeypatch.setattr(mcp_stdio, "_request_with_retry", fake)
    results = [mcp_stdio._handle_tools_call(c) for c in tool_calls]
    return sent, results


def _calls_via_sdk(monkeypatch, tool_calls):
    sdk = pytest.importorskip("awm.gateway.mcp_server_sdk")
    sent = []

    async def fake(method, path, json_body=None, headers=None, **kw):
        sent.append((method, path, json_body, dict(headers or {})))
        return {"result": "OK"}

    monkeypatch.setattr(sdk, "_request_with_retry", fake)

    async def run():
        return [await sdk.call_tool(c["name"], c["arguments"]) for c in tool_calls]

    results = asyncio.run(run())
    return sent, [[t.text for t in r] for r in results]


_DIRECT = {"name": "kb", "arguments": {"verb": "get", "args": {"id": 3}}}
_VIA_MORE = {"name": "more", "arguments": {
    "domain": "kb", "verb": "get", "args": {"id": 3}}}


def test_stdio_proxy_sends_more_as_the_direct_call(monkeypatch):
    monkeypatch.setenv("AWM_AS", "placed-9")
    sent, results = _calls_via_stdio(monkeypatch, [_DIRECT, _VIA_MORE])
    assert sent[0] == sent[1]
    assert sent[1][1:3] == ("/invoke", {"name": "kb", "args": {
        "verb": "get", "args": {"id": 3}}})
    assert sent[1][3]["X-Awm-As"] == "placed-9"
    assert "X-Awm-Session-Pid" in sent[1][3]
    assert results[0] == results[1]


def test_sdk_proxy_sends_more_as_the_direct_call(monkeypatch):
    monkeypatch.setenv("AWM_AS", "placed-9")
    sent, results = _calls_via_sdk(monkeypatch, [_DIRECT, _VIA_MORE])
    assert sent[0] == sent[1]
    assert sent[1][1:3] == ("/invoke", {"name": "kb", "args": {
        "verb": "get", "args": {"id": 3}}})
    assert sent[1][3]["X-Awm-As"] == "placed-9"
    assert "X-Awm-Session-Pid" in sent[1][3]
    assert results[0] == results[1]


def test_both_proxies_send_a_domainless_more_to_the_gateway_for_listing(
        monkeypatch):
    call = {"name": "more", "arguments": {}}
    stdio_sent, _ = _calls_via_stdio(monkeypatch, [call])
    sdk_sent, _ = _calls_via_sdk(monkeypatch, [call])
    assert stdio_sent[0][2] == sdk_sent[0][2] == {"name": "more", "args": {}}


def test_both_proxies_ask_for_the_tiered_surface(monkeypatch):
    asked = []

    def fake(method, path, params=None, **kw):
        asked.append(params)
        return {"tools": []}

    monkeypatch.setattr(mcp_stdio, "_request_with_retry", fake)
    mcp_stdio._handle_tools_list()
    sdk = pytest.importorskip("awm.gateway.mcp_server_sdk")

    async def fake_async(method, path, params=None, **kw):
        asked.append(params)
        return {"tools": []}

    monkeypatch.setattr(sdk, "_request_with_retry", fake_async)
    asyncio.run(sdk.list_tools())
    assert asked == [{"view": "domains", "peers": "1", "tiers": "1"}] * 2


# ---------------------------------------------------------------------------
# The HTTP route the proxies call
# ---------------------------------------------------------------------------


def _get_tools(**params):
    from fastapi.testclient import TestClient

    from awm.gateway.server import app

    # No ``with``: the lifespan would bootstrap the services under the tmp root.
    resp = TestClient(app).get("/tools", params={"view": "domains", **params})
    assert resp.status_code == 200
    return [t["name"] for t in resp.json()["tools"]]


def test_tools_route_with_tiers_returns_more_and_hides_discoverable(tiered):
    names = _get_tools(peers=1, tiers=1)
    assert "more" in names and "providersOf" in names and "scope" in names
    assert "kb" not in names and "orch" not in names


def test_tools_route_without_tiers_is_unchanged(tiered):
    names = _get_tools(peers=1)
    assert {"kb", "orch", "scope", "providersOf"} <= set(names)
    assert "more" not in names
    plain = _get_tools()
    assert "more" not in plain and "providersOf" not in plain
    assert _get_tools(tiers=1) == plain
