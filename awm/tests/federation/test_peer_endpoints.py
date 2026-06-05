"""Tests for the peer endpoints column, federation _resolve fallback,
peer-token SSH bootstrap, and /peer/* route cross-check (require_peer_bearer)."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.federation, pytest.mark.slow]

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def peer_workspace(awm_workspace):
    """The default workspace fixture initializes the DB at the latest schema
    version; that gives us the endpoints + tls_fingerprint columns on peers."""
    return awm_workspace


class TestEndpointPersistence:
    def test_add_peer_with_no_endpoints_synthesizes_ssh_fallback(
        self, peer_workspace,
    ):
        from awm.services.network import peers
        peers.add_peer("p1", "host.example", remote_port=7820)
        row = peers.get_peer("p1")
        assert row["endpoints"] == [
            {"kind": "ssh", "alias": "host.example", "port": 7820},
        ]
        assert row["tls_fingerprint"] is None

    def test_add_peer_with_direct_endpoint_persists(self, peer_workspace):
        from awm.services.network import peers
        peers.add_peer(
            "p2", "host.example",
            endpoints=[{"kind": "direct", "url": "https://10.1.2.3:7820"}],
            tls_fingerprint="deadbeef" * 8,
        )
        row = peers.get_peer("p2")
        assert row["endpoints"] == [
            {"kind": "direct", "url": "https://10.1.2.3:7820"},
        ]
        assert row["tls_fingerprint"] == "deadbeef" * 8

    def test_add_peer_normalizes_ssh_endpoint(self, peer_workspace):
        from awm.services.network import peers
        peers.add_peer(
            "p3", "host.example",
            endpoints=[{"kind": "ssh", "alias": "capella", "port": 7820}],
        )
        row = peers.get_peer("p3")
        assert row["endpoints"] == [
            {"kind": "ssh", "alias": "capella", "port": 7820},
        ]

    def test_add_peer_rejects_bad_endpoint_kind(self, peer_workspace):
        from awm.services.network import peers
        with pytest.raises(ValueError, match="unknown endpoint kind"):
            peers.add_peer(
                "p4", "host.example",
                endpoints=[{"kind": "smoke-signal", "url": "x"}],
            )


class TestResolveOrder:
    def test_resolves_first_direct_endpoint(
        self, peer_workspace, monkeypatch, tmp_path,
    ):
        from awm.services.network import peers, federation

        # Token must exist for load_peer_token to succeed.
        token_file = tmp_path / "p5.token"
        token_file.write_text("peer-token-xyz\n")
        peers.install_peer_token("p5", token_file)

        peers.add_peer(
            "p5", "host.example",
            endpoints=[
                {"kind": "direct", "url": "https://10.0.0.5:7820"},
                {"kind": "ssh", "alias": "host.example", "port": 7820},
            ],
        )

        # Federation must NOT actually attempt the SSH tunnel because the
        # direct endpoint comes first.
        called_ssh = []

        def fake_acquire(peer_id):
            called_ssh.append(peer_id)
            raise RuntimeError("should not be called")

        monkeypatch.setattr(
            "awm.services.network.ssh_tunnel.acquire_tunnel", fake_acquire,
        )

        # Clear the in-process preference cache between tests.
        federation._preferred_endpoint.clear()
        # _resolve returns local auth.token (not the peer's stored token)
        # because /peer/* routes use require_peer_bearer which expects
        # the sender's own bearer + X-Awm-From. See federation._resolve
        # docstring.
        from awm.services import auth as auth_svc
        base, token = federation._resolve("p5")
        assert base == "https://10.0.0.5:7820"
        assert token == auth_svc.local_token()
        assert called_ssh == []


# ---------------------------------------------------------------------------
# SSH peer-token bootstrap
# ---------------------------------------------------------------------------

class TestInstallPeerTokenViaSSH:
    def test_writes_token_with_0600_when_ssh_succeeds(
        self, peer_workspace, monkeypatch,
    ):
        from awm.services.network import peers

        class _R:
            returncode = 0
            stdout = "remote-secret-token\n"
            stderr = ""

        def fake_run(cmd, **kw):  # noqa: ARG001
            return _R()

        monkeypatch.setattr("subprocess.run", fake_run)
        dest = peers.install_peer_token_via_ssh("alpha", "alpha-host")
        assert dest.exists()
        assert (dest.stat().st_mode & 0o777) == 0o600
        assert dest.read_text().strip() == "remote-secret-token"

    def test_raises_on_ssh_failure(self, peer_workspace, monkeypatch):
        from awm.services.network import peers

        class _R:
            returncode = 255
            stdout = ""
            stderr = "Permission denied"

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: _R())
        with pytest.raises(RuntimeError, match="ssh.*failed"):
            peers.install_peer_token_via_ssh("alpha", "alpha-host")

    def test_raises_on_empty_remote_token(self, peer_workspace, monkeypatch):
        from awm.services.network import peers

        class _R:
            returncode = 0
            stdout = "\n"
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: _R())
        with pytest.raises(RuntimeError, match="empty"):
            peers.install_peer_token_via_ssh("alpha", "alpha-host")

    def test_rejects_missing_alias(self, peer_workspace):
        from awm.services.network import peers
        with pytest.raises(ValueError):
            peers.install_peer_token_via_ssh("alpha", "")


# ---------------------------------------------------------------------------
# /peer/* route cross-check via require_peer_bearer
# ---------------------------------------------------------------------------

@pytest.fixture()
def peer_client(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    (awm_dir / "auth.token").write_text("local-secret\n")
    peers_dir = awm_dir / "peers"
    peers_dir.mkdir()
    (peers_dir / "alpha.token").write_text("alpha-secret\n")
    (peers_dir / "bravo.token").write_text("bravo-secret\n")

    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", awm_dir / "auth.token")
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", awm_dir / "awm-exposed.pid")
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", awm_dir / "awm-exposed.log")
    monkeypatch.setattr(
        "awm.services.agent_instances.PROJECTS_DIR",
        awm_workspace["projects_dir"],
    )

    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})
    from awm.services import agent_instances
    agent_instances._registry_by_id.clear(); agent_instances._by_scope.clear(); agent_instances._by_agent_id.clear()
    agent_instances._by_scope.clear()
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)

    # Register the peers so middleware-level "unknown peer" checks pass.
    from awm.services.network import peers as peer_svc
    peer_svc.add_peer("alpha", "alpha-host")
    peer_svc.add_peer("bravo", "bravo-host")

    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestPeerRouteAuth:
    def test_missing_x_awm_from_400(self, peer_client):
        r = peer_client.post(
            "/peer/inbox",
            headers={"Authorization": "Bearer alpha-secret"},
            json={"sender": "x", "recipient": "y", "body": "hi"},
        )
        assert r.status_code == 400
        assert "X-Awm-From" in r.json()["detail"]

    def test_bearer_mismatch_rejected(self, peer_client):
        # alpha's bearer presented but claiming to be bravo → 401.
        r = peer_client.post(
            "/peer/inbox",
            headers={
                "Authorization": "Bearer alpha-secret",
                "X-Awm-From": "bravo",
            },
            json={"sender": "x", "recipient": "y", "body": "hi"},
        )
        assert r.status_code == 401

    def test_local_token_cannot_impersonate_peer(self, peer_client):
        r = peer_client.post(
            "/peer/inbox",
            headers={
                "Authorization": "Bearer local-secret",
                "X-Awm-From": "alpha",
            },
            json={"sender": "x", "recipient": "y", "body": "hi"},
        )
        assert r.status_code == 401

    def test_matching_bearer_accepted(self, peer_client, awm_workspace):
        # We need a scope so messaging.send_message has somewhere to put it.
        # /peer/inbox calls messaging.send_message which validates the
        # recipient scope exists; the easiest way to verify auth passes is
        # to use an intentionally-invalid body and assert we get a 400
        # from the *handler* (auth passed) rather than 401 from the dep.
        r = peer_client.post(
            "/peer/inbox",
            headers={
                "Authorization": "Bearer alpha-secret",
                "X-Awm-From": "alpha",
            },
            json={"definitely": "not a MessageSendRequest"},
        )
        # 400 = auth passed, body invalid (which is what we want to assert).
        assert r.status_code == 400
        assert "invalid message body" in r.json()["detail"].lower() \
            or "field required" in str(r.json()).lower()
