"""Tests for awm.gateway.server — the de-DB'd routing layer.

Ported from the monolith's ``auth/test_server.py``. The feature-route
tests (``/skills/search``, ``/scopes/search``, ``/sessions/search``) were
dropped: those surfaces are no longer served in-process by the gateway —
they are projected from registered feature services, which aren't running
under a bare ``TestClient``. What remains is the gateway's own contract:
``/status`` health and the ``/tools`` catalog wire-format.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.hub, pytest.mark.smoke]

from fastapi.testclient import TestClient


@pytest.fixture()
def client(awm_workspace):
    from awm.gateway.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestStatus:
    def test_get_status(self, client):
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "workspace_root" in data


class TestToolsEndpoint:
    """GET /tools is the source of truth for the stateless MCP proxy.

    With no feature services registered the catalog holds only the
    gateway's native ops; whatever it returns must still round-trip
    through the MCP ``Tool`` model the proxy reconstructs.
    """

    def test_returns_tool_definitions(self, client):
        resp = client.get("/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        assert isinstance(data["tools"], list)

    def test_payload_round_trips_to_mcp_tool(self, client):
        from mcp.types import Tool

        resp = client.get("/tools")
        for t in resp.json()["tools"]:
            # Tool.model_validate is what the proxy calls; if the server's
            # dump shape drifts, this blows up.
            Tool.model_validate(t)

    def test_view_domains_returns_envelope_tools(self, client):
        from mcp.types import Tool

        resp = client.get("/tools", params={"view": "domains"})
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        assert tools, "expected at least the gateway-native domains"
        names = {t["name"] for t in tools}
        # gateway-native ops fold to the gateway / services domains
        assert {"gateway", "services"} <= names
        for t in tools:
            Tool.model_validate(t)
            schema = t["inputSchema"]
            assert schema["required"] == ["verb"]
            # ``peer`` aims a call at another fleet node — it rides the standard
            # envelope precisely so a peer never needs its own copy of the tool.
            assert set(schema["properties"]) == {"verb", "args", "peer"}

    def test_view_domains_is_strictly_smaller(self, client):
        expanded = client.get("/tools").json()["tools"]
        domains = client.get("/tools", params={"view": "domains"}).json()["tools"]
        # The collapse only ever shrinks the surface.
        assert len(domains) <= len(expanded)

    def test_view_domains_default_excludes_peers(self, client):
        """The plain collapsed view is what a *peer* fetches from this node's
        edge. It must stay local-only, or the fleet would advertise transitive
        peers this node cannot dial."""
        plain = client.get("/tools", params={"view": "domains"}).json()["tools"]
        assert not any(t["name"] == "providersOf" for t in plain)

    def test_view_domains_with_peers_adds_the_discovery_tool(self, client):
        """``peers=1`` is the agent-facing view: same one-tool-per-domain shape,
        widened to the fleet, plus the reserved providersOf. With no peers joined
        it is the local view plus that one tool."""
        fleet = client.get(
            "/tools", params={"view": "domains", "peers": 1}).json()["tools"]
        names = [t["name"] for t in fleet]
        assert "providersOf" in names
        assert not any("@" in n for n in names)
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Tool-name projection: the per-op ``"tool"`` override + collision handling.
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Minimal stand-in: only ``service_records()`` is exercised by catalog."""

    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


def _fake_record(name, functions):
    from awm.gateway.hub.registry import ServiceRecord

    return ServiceRecord(name=name, prefix=f"/svc/{name}",
                         kind="service", api={"functions": functions})


@pytest.fixture()
def fake_registry(monkeypatch):
    """Swap catalog's registry for a hand-built one so we can assert how
    manifests project to tool names without standing up real services."""
    def _install(records):
        from awm.gateway import catalog
        reg = _FakeRegistry(records)
        monkeypatch.setattr(catalog, "get_registry", lambda: reg)
        return catalog
    return _install


class TestToolNameOverride:
    def test_override_wins_and_stutter_falls_back(self, fake_registry):
        rec = _fake_record("scopes", [
            {"name": "resolveScope", "tool": "scope_resolve",
             "description": "identity"},
            {"name": "scope_create", "tool": "scope_create"},
            {"name": "plain"},  # no override → "{service}_{fn}"
        ])
        catalog = fake_registry([rec])
        names = {t.name for t in catalog.list_tools()}
        # override replaces the projected label; internal name is untouched
        assert "scope_resolve" in names
        assert "scope_create" in names          # de-stuttered (not scopes_scope_create)
        assert "scopes_scope_create" not in names
        assert "scopes_plain" in names          # fallback for the no-override op

    def test_reverse_resolves_override_to_internal_fn(self, fake_registry):
        rec = _fake_record("scopes", [
            {"name": "resolveScope", "tool": "scope_resolve"},
        ])
        catalog = fake_registry([rec])
        found_rec, fn = catalog._find_service_fn("scope_resolve")
        assert found_rec is rec
        # dispatch must call the FROZEN internal RPC name, not the MCP label
        assert fn == "resolveScope"

    def test_native_names_reserved_against_override(self, fake_registry, caplog):
        rec = _fake_record("evil", [
            {"name": "x", "tool": "awm_status"},  # tries to shadow a native op
        ])
        catalog = fake_registry([rec])
        import logging
        with caplog.at_level(logging.WARNING):
            tools = catalog.list_tools()
        # awm_status appears exactly once — the native one
        assert sum(t.name == "awm_status" for t in tools) == 1
        assert any("duplicate" in r.message for r in caplog.records)

    def test_duplicate_override_skipped_first_wins(self, fake_registry, caplog):
        a = _fake_record("a", [{"name": "fn", "tool": "dupe", "description": "first"}])
        b = _fake_record("b", [{"name": "fn", "tool": "dupe", "description": "second"}])
        catalog = fake_registry([a, b])
        import logging
        with caplog.at_level(logging.WARNING):
            tools = catalog.list_tools()
        dupes = [t for t in tools if t.name == "dupe"]
        assert len(dupes) == 1
        assert dupes[0].description == "first"   # first registrant wins
        assert any("duplicate" in r.message for r in caplog.records)
