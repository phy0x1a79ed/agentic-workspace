"""Middleware-gate tests for STANDBY leadership: 503+Location for /ui/* and
the login-flow entry points; pass-through for /peer/*, /status, etc."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from awm.services.leadership import state as ldr_state


@pytest.fixture()
def exposed_client(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    token_file.write_text("test-local-token\n")

    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", token_file)
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", awm_dir / "awm-exposed.pid")
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", awm_dir / "awm-exposed.log")
    monkeypatch.setattr(
        "awm.services.agent_instances.PROJECTS_DIR",
        awm_workspace["projects_dir"],
    )
    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})
    _auth._challenges.clear()
    from awm.services import agent_instances
    agent_instances._registry.clear()
    agent_instances._by_scope.clear()
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)

    ldr_state.reset_for_test()

    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    ldr_state.reset_for_test()


def _force_standby(leader_peer_id: str | None = None) -> None:
    ldr_state.configure("xps", 20)
    s = ldr_state.get_state()
    s.leadership = "STANDBY"
    s.current_leader = leader_peer_id


def _force_active() -> None:
    ldr_state.configure("xps", 20)
    s = ldr_state.get_state()
    s.leadership = "ACTIVE"
    s.current_leader = "xps"


def test_ui_in_active_passes_through(exposed_client):
    _force_active()
    r = exposed_client.get("/ui/", follow_redirects=False)
    # 200 OK or 404 (no static), but not 503 — we're ACTIVE.
    assert r.status_code != 503


def test_ui_in_standby_with_known_leader_returns_503_with_location(
    exposed_client, awm_workspace,
):
    from awm.services.network import peers as peer_svc
    peer_svc.add_peer(
        "capella", "self",
        endpoints=[{"kind": "direct", "url": "https://10.0.0.1:7820"}],
        peer_priority=10,
    )
    _force_standby("capella")
    r = exposed_client.get("/ui/", follow_redirects=False)
    assert r.status_code == 503
    assert r.headers.get("location") == "https://10.0.0.1:7820/ui/"


def test_ui_in_standby_without_known_leader_returns_503_no_location(exposed_client):
    _force_standby(None)
    r = exposed_client.get("/ui/", follow_redirects=False)
    assert r.status_code == 503
    assert "location" not in {k.lower() for k in r.headers.keys()}


def test_auth_bootstrap_in_standby_redirects(exposed_client, awm_workspace):
    from awm.services.network import peers as peer_svc
    peer_svc.add_peer(
        "capella", "self",
        endpoints=[{"kind": "direct", "url": "https://10.0.0.1:7820"}],
        peer_priority=10,
    )
    _force_standby("capella")
    r = exposed_client.get("/auth/bootstrap?ot=abc", follow_redirects=False)
    assert r.status_code == 503
    assert "10.0.0.1" in r.headers.get("location", "")


def test_status_in_standby_stays_open(exposed_client):
    """/status must stay reachable in STANDBY — it's the peer-probe surface."""
    _force_standby(None)
    r = exposed_client.get(
        "/status",
        headers={"Authorization": "Bearer test-local-token"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["leadership_state"] == "STANDBY"


def test_peer_routes_in_standby_stay_open(exposed_client):
    """/peer/* must stay reachable in STANDBY — federation never pauses."""
    _force_standby(None)
    # /peer/inbox without proper headers will 4xx (not 503) — what matters
    # is that the leadership gate didn't shortcut it.
    r = exposed_client.post("/peer/inbox", json={})
    assert r.status_code != 503


def test_whoami_in_standby_stays_open(exposed_client):
    """/auth/whoami must keep working in STANDBY so existing browsers can
    clear state cleanly without forced redirect."""
    _force_standby(None)
    r = exposed_client.get(
        "/auth/whoami",
        headers={"Authorization": "Bearer test-local-token"},
    )
    assert r.status_code == 200
