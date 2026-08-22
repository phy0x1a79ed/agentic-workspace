"""Unit tests for the collapsed per-domain MCP projection (catalog.py).

The expanded per-verb surface (``list_tools``) keeps its own coverage; this
file exercises the *parallel* ``view=domains`` projection added alongside it:
the domain folding, the reserved ``describe`` verb, and the domain-aware
dispatch branch (verb → internal-fn resolution, native-domain routing, the
``"verb" in args`` guard). All synchronous against a stub registry so no hub
needs to spin up.
"""

from __future__ import annotations

import json

import pytest

from awm.gateway import catalog
from awm.gateway.hub.registry import ServiceRecord


# ---------------------------------------------------------------------------
# Fixtures: a stub registry the catalog reads instead of the live singleton
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


def _rec(name: str, prefix: str, functions: list[dict], service_id: str) -> ServiceRecord:
    return ServiceRecord(
        name=name, prefix=prefix, kind="service",
        api={"functions": functions}, service_id=service_id,
    )


@pytest.fixture()
def scopes_rec():
    """One service projecting THREE domains (scope / project / ref), including a
    name≠tool divergence: tool ``scope_refresh`` → internal fn ``awm_refresh``."""
    return _rec("scopes", "/svc/scopes", [
        {"name": "search", "tool": "scope_search",
         "description": "Search scopes.",
         "params": [{"name": "q", "type": "string", "required": True}]},
        {"name": "awm_refresh", "tool": "scope_refresh",
         "description": "Refresh a scope.",
         "params": [{"name": "scope", "type": "string", "required": True}]},
        {"name": "create_project", "tool": "project_create",
         "description": "Create a project.", "params": []},
        {"name": "resolve_ref", "tool": "ref_resolve",
         "description": "Resolve a ref.", "params": []},
    ], "sid-scopes")


@pytest.fixture()
def patched_registry(monkeypatch, scopes_rec):
    stub = _StubRegistry([scopes_rec])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    return stub


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def test_domain_tool_envelope(patched_registry):
    tools = {t.name: t for t in catalog.list_domain_tools()}
    # one service → three distinct domains
    assert {"scope", "project", "ref"} <= set(tools)
    scope = tools["scope"]
    assert scope.inputSchema["required"] == ["verb"]
    # ``peer`` rides the standard envelope beside verb/args — it is how a call is
    # aimed at another fleet node, instead of that node getting its own tool.
    assert set(scope.inputSchema["properties"]) == {"verb", "args", "peer"}
    assert scope.inputSchema["properties"]["args"]["type"] == "object"
    # verbs are advertised in the free-text description (cheap discovery) and
    # enumerated on ``verb`` so a peer reading this catalog gets them structurally
    assert "search" in scope.description and "refresh" in scope.description
    assert {"search", "refresh"} <= set(scope.inputSchema["properties"]["verb"]["enum"])


def test_envelope_constant_not_mutated_across_domains(patched_registry):
    """Each domain's enum is its own — a shallow copy would have every domain
    inherit whichever one was projected last."""
    tools = {t.name: t for t in catalog.list_domain_tools()}
    assert (tools["scope"].inputSchema["properties"]["verb"]["enum"]
            != tools["project"].inputSchema["properties"]["verb"]["enum"])
    assert "enum" not in catalog._DOMAIN_INPUT_SCHEMA["properties"]["verb"]


def test_native_domains_present(patched_registry):
    tools = {t.name: t for t in catalog.list_domain_tools()}
    # gateway-native ops fold by cli_group → gateway / services domains
    assert "gateway" in tools and "services" in tools


def test_domain_count_is_small(patched_registry):
    # The whole point: a handful of domains, not ~71 verb-tools.
    assert len(catalog.list_domain_tools()) <= 12


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def test_describe_lists_verbs_with_schemas(patched_registry):
    out = catalog._describe_domain("scope")
    verbs = {v["verb"]: v for v in out["verbs"]}
    assert {"search", "refresh"} <= set(verbs)
    # full param schema is carried through, identical to the expanded tool
    assert verbs["search"]["params"]["properties"]["q"]["type"] == "string"
    assert verbs["search"]["params"]["required"] == ["q"]


def test_describe_narrows_to_one_verb(patched_registry):
    out = catalog._describe_domain("scope", "refresh")
    assert [v["verb"] for v in out["verbs"]] == ["refresh"]


def test_describe_unknown_domain_or_verb(patched_registry):
    with pytest.raises(ValueError):
        catalog._describe_domain("nope")
    with pytest.raises(ValueError):
        catalog._describe_domain("scope", "nonexistent")


def test_native_describe(patched_registry):
    out = catalog._describe_domain("gateway")
    verbs = {v["verb"] for v in out["verbs"]}
    assert "status" in verbs  # awm_status → gateway/status


# ---------------------------------------------------------------------------
# surfaces gating — a CLI/HTTP-only verb is hidden from the MCP domain surface
# but stays on the expanded list_tools() the CLI generator reads.
# ---------------------------------------------------------------------------


@pytest.fixture()
def surfaced_rec():
    """A service whose write verb is declared CLI/HTTP-only via ``surfaces``."""
    return _rec("writing", "/svc/writing", [
        {"name": "search", "tool": "writing_search",
         "description": "Search the corpus.", "params": []},
        {"name": "add", "tool": "writing_add",
         "description": "Add a sample.", "surfaces": ["cli", "http"],
         "params": [{"name": "text", "type": "string", "required": True}]},
    ], "sid-writing")


def test_cli_only_verb_absent_from_domain_surface(monkeypatch, surfaced_rec):
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([surfaced_rec]))
    out = catalog._describe_domain("writing")
    verbs = {v["verb"] for v in out["verbs"]}
    assert "search" in verbs  # read verb projected to MCP
    assert "add" not in verbs  # write verb hidden from MCP


def test_cli_only_verb_present_in_expanded_list_tools(monkeypatch, surfaced_rec):
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([surfaced_rec]))
    names = {t.name for t in catalog.list_tools()}
    # both verbs stay on the expanded surface the CLI generator + flat /invoke read
    assert {"writing_search", "writing_add"} <= names


async def test_cli_only_verb_rejected_via_mcp_domain_dispatch(monkeypatch, surfaced_rec):
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([surfaced_rec]))
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: _FakeChannel())
    # an agent calling writing(verb="add") over MCP is rejected (verb not in catalog)
    with pytest.raises(ValueError):
        await catalog.dispatch("writing", {"verb": "add", "args": {"text": "x"}})


async def test_manifest_timeout_threaded_to_rpc(monkeypatch):
    rec = _rec("slow", "/svc/slow", [
        {"name": "grind", "tool": "slow_grind",
         "description": "Slow op.", "timeout": 300, "params": []},
        {"name": "quick", "tool": "slow_quick",
         "description": "Fast op.", "params": []},
    ], "sid-slow")
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([rec]))
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    # declared timeout is threaded into ch.call
    await catalog.dispatch("slow", {"verb": "grind", "args": {}})
    assert ch.calls[-1][3] == 300.0
    # a verb with no timeout leaves it at the ControlChannel default (None here)
    await catalog.dispatch("slow", {"verb": "quick", "args": {}})
    assert ch.calls[-1][3] is None


async def test_cli_only_verb_still_dispatches_flat_by_name(monkeypatch, surfaced_rec):
    # the CLI's flat /invoke {name:"writing_add"} path stays functional
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([surfaced_rec]))
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    raw = await catalog.dispatch("writing_add", {"text": "hello"})
    out = json.loads(raw)
    assert out["fn"] == "add" and out["args"] == {"text": "hello"}


# ---------------------------------------------------------------------------
# Duplicate verb → first-wins
# ---------------------------------------------------------------------------


def test_duplicate_verb_first_wins(monkeypatch):
    a = _rec("svcA", "/svc/svcA", [
        {"name": "dup", "tool": "thing_go", "description": "A wins.", "params": []},
    ], "sid-a")
    b = _rec("svcB", "/svc/svcB", [
        {"name": "dup", "tool": "thing_go", "description": "B loses.", "params": []},
    ], "sid-b")
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([a, b]))
    out = catalog._describe_domain("thing")
    rows = [v for v in out["verbs"] if v["verb"] == "go"]
    assert len(rows) == 1 and rows[0]["description"] == "A wins."


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self):
        self.calls = []

    async def call(self, fn, args, as_=None, timeout=None):
        self.calls.append((fn, args, as_, timeout))
        return {"fn": fn, "args": args, "as_": as_, "timeout": timeout}


async def test_dispatch_service_verb_resolves_internal_fn(monkeypatch, patched_registry):
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    # scope/refresh must route to the INTERNAL fn name (awm_refresh), not the verb
    raw = await catalog.dispatch(
        "scope", {"verb": "refresh", "args": {"scope": "demo"}}, as_="unit-x")
    out = json.loads(raw)
    assert out["fn"] == "awm_refresh"
    assert out["args"] == {"scope": "demo"}
    assert out["as_"] == "unit-x"


async def test_dispatch_describe_no_service_roundtrip(monkeypatch, patched_registry):
    # describe must never hit the control channel
    def _boom(sid):
        raise AssertionError("describe should not reach a service")
    monkeypatch.setattr(catalog.rpc, "get_control", _boom)
    raw = await catalog.dispatch("scope", {"verb": "describe"})
    assert "search" in raw and "refresh" in raw


async def test_dispatch_unknown_verb(monkeypatch, patched_registry):
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: _FakeChannel())
    with pytest.raises(ValueError):
        await catalog.dispatch("scope", {"verb": "does_not_exist", "args": {}})


async def test_dispatch_native_verb(patched_registry):
    # gateway/status is a sync native op (reads config) — exercises the native
    # branch end to end without a service channel.
    raw = await catalog.dispatch("gateway", {"verb": "status"})
    out = json.loads(raw)
    assert out["status"] == "ok"


async def test_flat_dispatch_still_works_without_verb(monkeypatch, patched_registry):
    # A flat by-name call (no 'verb' key) must bypass the domain branch and use
    # the existing reverse-lookup path — the CLI/HTTP surface depends on this.
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    raw = await catalog.dispatch("scope_search", {"q": "hi"})
    out = json.loads(raw)
    assert out["fn"] == "search" and out["args"] == {"q": "hi"}


async def test_domain_name_without_verb_is_not_hijacked(monkeypatch, patched_registry):
    # 'scope' with no 'verb' key is NOT a domain call; it falls through to flat
    # dispatch, which has no tool literally named 'scope' → ValueError.
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: _FakeChannel())
    with pytest.raises(ValueError):
        await catalog.dispatch("scope", {"something": 1})
