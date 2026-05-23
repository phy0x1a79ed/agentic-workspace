"""Tests for the peer endpoints column + federation _resolve fallback order."""

from __future__ import annotations

import pytest


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
        base, token = federation._resolve("p5")
        assert base == "https://10.0.0.5:7820"
        assert token == "peer-token-xyz"
        assert called_ssh == []
