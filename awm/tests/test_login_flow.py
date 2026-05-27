"""End-to-end tests for the one-shot login flow:

  POST /auth/mint        (loopback-only, returns nonce + URL)
  GET  /auth/bootstrap   (consumes nonce, sets cookie, 302→/ui/)
  GET  /auth/whoami      (authenticated; returns awm_user from cookie)

These hit the FastAPI app via TestClient — no real HTTPS / sockets.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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

    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def good_token():
    return "test-local-token"


class TestAuthMint:
    def test_mints_nonce_and_url(self, exposed_client, good_token):
        r = exposed_client.post(
            "/auth/mint",
            json={"awm_user": "tony"},
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["awm_user"] == "tony"
        assert body["nonce"]
        assert "/auth/bootstrap?ot=" in body["url"]
        assert body["expires_in_s"] > 0

    def test_defaults_awm_user_to_operator(self, exposed_client, good_token):
        r = exposed_client.post(
            "/auth/mint",
            json={},
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200
        assert r.json()["awm_user"] == "operator"

    def test_requires_bearer(self, exposed_client):
        r = exposed_client.post("/auth/mint", json={"awm_user": "a"})
        assert r.status_code == 401


class TestAuthBootstrap:
    def _mint(self, client, good_token, user="tony"):
        r = client.post(
            "/auth/mint",
            json={"awm_user": user},
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200
        return r.json()["nonce"]

    def test_sets_cookies_and_redirects(self, exposed_client, good_token):
        nonce = self._mint(exposed_client, good_token, "tony")
        r = exposed_client.get(
            f"/auth/bootstrap?ot={nonce}", follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"] == "/ui/"
        cookies = r.cookies
        # The HttpOnly bearer cookie carries the local token itself.
        assert cookies.get("awm_session") == "test-local-token"
        assert cookies.get("awm_as") == "tony"

    def test_nonce_single_use(self, exposed_client, good_token):
        nonce = self._mint(exposed_client, good_token)
        r1 = exposed_client.get(
            f"/auth/bootstrap?ot={nonce}", follow_redirects=False,
        )
        assert r1.status_code == 302
        r2 = exposed_client.get(
            f"/auth/bootstrap?ot={nonce}", follow_redirects=False,
        )
        assert r2.status_code == 401

    def test_unknown_nonce_rejected(self, exposed_client):
        r = exposed_client.get("/auth/bootstrap?ot=garbage")
        assert r.status_code == 401

    def test_expired_nonce_rejected(self, exposed_client, good_token):
        from awm.services import auth as _auth
        nonce = _auth.mint_challenge("tony", ttl=-1)
        r = exposed_client.get(
            f"/auth/bootstrap?ot={nonce}", follow_redirects=False,
        )
        assert r.status_code == 401


class TestAuthWhoami:
    def test_returns_awm_user_from_cookie(self, exposed_client, good_token):
        r = exposed_client.get(
            "/auth/whoami",
            headers={"Authorization": f"Bearer {good_token}"},
            cookies={"awm_as": "tony"},
        )
        assert r.status_code == 200
        assert r.json()["awm_user"] == "tony"

    def test_unauthenticated_rejected(self, exposed_client):
        r = exposed_client.get("/auth/whoami")
        assert r.status_code == 401

    def test_defaults_to_operator(self, exposed_client, good_token):
        r = exposed_client.get(
            "/auth/whoami",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert r.status_code == 200
        assert r.json()["awm_user"] == "operator"
