"""Empty-registry path = byte-identical to today (G2).

Confirms HubRoutingMiddleware adds no observable behavior when no
service is registered.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.hub, pytest.mark.slow]

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty registry — the singleton is
    process-wide. Replace the instance to avoid cross-loop Lock state."""
    from awm.services.hub import registry as _reg_mod
    _reg_mod._singleton = _reg_mod.Registry()
    yield
    _reg_mod._singleton = _reg_mod.Registry()


@pytest.fixture()
def exposed_workspace(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    monkeypatch.setattr("awm.config.ACCESS_LOG", awm_dir / "access.log")
    return {**awm_workspace, "token_file": token_file}


@pytest.fixture()
def good_token(exposed_workspace):
    token = "passthrough-test-token"
    exposed_workspace["token_file"].write_text(token + "\n")
    return token


@pytest.fixture()
def client(exposed_workspace):
    from awm.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestEmptyRegistryPassthrough:
    def test_status_unchanged(self, client, good_token):
        r = client.get("/status",
                       headers={"Authorization": f"Bearer {good_token}"})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_hub_list_works(self, client, good_token):
        # Empty registry: hub control plane still reachable.
        r = client.get("/hub/services",
                       headers={"Authorization": f"Bearer {good_token}"})
        assert r.status_code == 200
        assert r.json() == {"services": []}


class TestHubControlPlaneNotProxied:
    """/hub/* must never be proxied, even if a registered prefix would
    shadow it (e.g. someone registers '/'). Otherwise the lease WS itself
    becomes unreachable and the system can't recover."""

    def test_root_prefix_does_not_shadow_hub(self, client, good_token):
        # Register via the live API so we don't have to think about which
        # loop owns the Lock.
        r = client.post(
            "/hub/register",
            json={"name": "greedy", "prefix": "/",
                  "url": "http://127.0.0.1:1"},
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        r = client.get(
            "/hub/services",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["services"]]
        assert "greedy" in names
