"""The replication link: where it may listen, and where it may dial.

One vault now lives on three machines, and the traffic that keeps them equal
speaks to a Trilium whose own authentication is off. So both ends of the link
are loopback, and both are asserted rather than documented: the forward's
listening socket here, and the address the vault child is told to sync to.
"""

from __future__ import annotations

import pytest

from awm.trilium import instances, server, sync

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture
def as_client(monkeypatch):
    """This node configured as a spoke of `sirius`."""
    monkeypatch.setenv("TRILIUM_SYNC_HUB", "sirius")
    monkeypatch.setenv("TRILIUM_SYNC_TUNNEL_PORT", "12611")
    monkeypatch.delenv("TRILIUM_SYNC_HUB_PORT", raising=False)


def test_the_sync_target_is_loopback_whatever_the_environment_says(
        as_client, monkeypatch):
    """Same reasoning as the bind: an inherited value must not be able to send
    an unauthenticated vault's traffic off the machine."""
    monkeypatch.setenv("TRILIUM_SYNC_SYNCSERVERHOST", "http://vault.example:8080")
    env = server.child_env(instances.VAULT)
    assert env["TRILIUM_SYNC_SYNCSERVERHOST"] == "http://127.0.0.1:12611"


def test_the_forward_listens_on_loopback_only(as_client):
    """`ssh -L 12611:...` without an address obeys `GatewayPorts`, which a
    system-wide config can widen to every interface. Spelled out, it cannot."""
    cmd = sync.tunnel_cmd()
    spec = cmd[cmd.index("-L") + 1]
    assert spec == f"127.0.0.1:12611:127.0.0.1:{instances.UPSTREAM_PORT}"
    assert "0.0.0.0" not in " ".join(cmd)


def test_the_forward_exits_rather_than_carrying_nothing(as_client):
    """Without `ExitOnForwardFailure` a refused bind leaves ssh up and the
    vault reporting a sync host that answers nobody. The supervision loop can
    see an exit; it cannot see that."""
    cmd = sync.tunnel_cmd()
    assert "ExitOnForwardFailure=yes" in cmd


def test_the_tunnel_may_not_take_the_vault_s_own_port(as_client, monkeypatch):
    """They are both loopback ports on one machine, and the collision would
    show up as a vault that will not start."""
    monkeypatch.setenv("TRILIUM_SYNC_TUNNEL_PORT", str(instances.UPSTREAM_PORT))
    with pytest.raises(RuntimeError, match="the vault's own port"):
        sync.tunnel_cmd()
