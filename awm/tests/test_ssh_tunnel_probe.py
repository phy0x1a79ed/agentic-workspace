"""Tests for ssh_tunnel._probe and Tunnel.local_url scheme.

awm-exposed listens HTTPS-only (auto-bootstrapped TLS in
exposed.run_exposed_server). The loopback probe and the local_url
handed to federation must therefore use https + verify=False so the
SSH-tunneled call lands on the TLS socket without certificate
verification (the SSH channel is already authenticated).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from awm.services.network import ssh_tunnel


class TestProbe:
    def test_probe_uses_https_url(self):
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return MagicMock(status_code=401)

        with patch("awm.services.network.ssh_tunnel.httpx.get", side_effect=fake_get):
            assert ssh_tunnel._probe(54321) is True
        assert captured["url"].startswith("https://127.0.0.1:54321"), captured["url"]
        assert captured["url"].endswith("/peer")

    def test_probe_passes_verify_false(self):
        captured = {}

        def fake_get(url, **kwargs):
            captured.update(kwargs)
            return MagicMock(status_code=200)

        with patch("awm.services.network.ssh_tunnel.httpx.get", side_effect=fake_get):
            ssh_tunnel._probe(12345)
        assert captured.get("verify") is False, captured

    def test_probe_accepts_503(self):
        with patch("awm.services.network.ssh_tunnel.httpx.get",
                   return_value=MagicMock(status_code=503)):
            assert ssh_tunnel._probe(11111) is True

    def test_probe_rejects_5xx(self):
        with patch("awm.services.network.ssh_tunnel.httpx.get",
                   return_value=MagicMock(status_code=500)):
            assert ssh_tunnel._probe(11111) is False

    def test_probe_returns_false_on_httpx_error(self):
        with patch("awm.services.network.ssh_tunnel.httpx.get",
                   side_effect=httpx.ConnectError("nope")):
            assert ssh_tunnel._probe(22222) is False


class TestTunnelLocalUrl:
    def test_local_url_uses_https(self):
        t = ssh_tunnel.Tunnel(
            peer_id="p", local_port=23456, sock_path="/tmp/x.sock",
            opened_at=0.0, remote_port=7820, ssh_alias="p",
        )
        assert t.local_url == "https://127.0.0.1:23456"
