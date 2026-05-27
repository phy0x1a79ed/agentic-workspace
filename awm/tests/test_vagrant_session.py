"""Tests for ensure_vagrant_session + POST /vagrant/session endpoint."""

from __future__ import annotations

import pytest

from awm.config import VAGRANT_PROJECT
from awm.services import scopes as scopes_svc
from awm.services import rooms as rooms_svc


@pytest.fixture
def vagrant_bootstrap(awm_workspace, monkeypatch):
    import shutil as _shutil
    real_which = _shutil.which
    monkeypatch.setattr(
        "awm.services.scopes.shutil.which",
        lambda name: None if name == "gh" else real_which(name),
    )
    scopes_svc.ensure_vagrant_repo()
    return awm_workspace


# ---------------------------------------------------------------------------
# ensure_vagrant_session()
# ---------------------------------------------------------------------------

def test_ensure_vagrant_session_creates_scope_and_room(vagrant_bootstrap):
    scope_uuid, room_id = scopes_svc.ensure_vagrant_session("user:alice")
    assert scope_uuid
    assert room_id

    # Scope row exists under the sentinel project.
    listed = scopes_svc.list_scopes(project=VAGRANT_PROJECT)
    assert len(listed.scopes) == 1
    assert listed.scopes[0].scope == "user-user-alice"

    # Room exists and is active.
    room = rooms_svc.get_room(room_id)
    assert room is not None
    assert room.status == "active"


def test_ensure_vagrant_session_is_idempotent(vagrant_bootstrap):
    first = scopes_svc.ensure_vagrant_session("user:alice")
    second = scopes_svc.ensure_vagrant_session("user:alice")
    assert first == second


def test_ensure_vagrant_session_distinct_users_get_distinct_rooms(vagrant_bootstrap):
    a_scope, a_room = scopes_svc.ensure_vagrant_session("user:alice")
    b_scope, b_room = scopes_svc.ensure_vagrant_session("user:bob")
    assert a_scope != b_scope
    assert a_room != b_room


def test_ensure_vagrant_session_recreates_room_if_closed(vagrant_bootstrap):
    _, room_id = scopes_svc.ensure_vagrant_session("user:alice")
    # Force the room closed and call again — should mint a new room.
    rooms_svc.close_room(room_id)
    _, room_id_after = scopes_svc.ensure_vagrant_session("user:alice")
    assert room_id_after != room_id


def test_ensure_vagrant_session_without_bootstrap_raises(awm_workspace):
    with pytest.raises(FileNotFoundError, match="awm vagrant-init"):
        scopes_svc.ensure_vagrant_session("user:alice")


# ---------------------------------------------------------------------------
# POST /vagrant/session — wire the endpoint via the exposed app
# ---------------------------------------------------------------------------

@pytest.fixture
def exposed_workspace(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", token_file)
    monkeypatch.setattr("awm.config.ACCESS_LOG", awm_dir / "access.log")
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", awm_dir / "awm-exposed.pid")
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", awm_dir / "awm-exposed.log")
    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)
    return {**awm_workspace, "token_file": token_file}


@pytest.fixture
def good_token(exposed_workspace):
    token = "test-secret-token-abc123"
    exposed_workspace["token_file"].write_text(token + "\n")
    return token


@pytest.fixture
def exposed_client(exposed_workspace):
    from fastapi.testclient import TestClient
    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_post_vagrant_session_requires_auth(exposed_client, good_token):
    r = exposed_client.post("/vagrant/session")
    assert r.status_code == 401


def test_post_vagrant_session_returns_503_when_not_bootstrapped(
    exposed_client, good_token
):
    r = exposed_client.post(
        "/vagrant/session",
        headers={"Authorization": f"Bearer {good_token}"},
    )
    assert r.status_code == 503
    assert "vagrant-init" in r.json()["detail"]


def test_post_vagrant_session_returns_room(
    exposed_client, good_token, vagrant_bootstrap
):
    r = exposed_client.post(
        "/vagrant/session",
        headers={
            "Authorization": f"Bearer {good_token}",
            "X-Awm-As": "user:tester",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope_uuid"]
    assert body["room_id"]

    # Idempotent: second call returns the same room.
    r2 = exposed_client.post(
        "/vagrant/session",
        headers={
            "Authorization": f"Bearer {good_token}",
            "X-Awm-As": "user:tester",
        },
    )
    assert r2.status_code == 200
    assert r2.json() == body


