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
