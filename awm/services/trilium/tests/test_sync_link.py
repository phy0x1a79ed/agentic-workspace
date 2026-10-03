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


@pytest.fixture
def as_hub(monkeypatch):
    """This node with no hub configured, which is what makes it one."""
    monkeypatch.delenv("TRILIUM_SYNC_HUB", raising=False)


def test_a_node_with_no_hub_is_one(as_hub):
    """Absence of configuration is the whole discriminator, so the hub needs
    nothing set and cannot become a client of itself."""
    assert not sync.is_client()
    assert sync.TUNNEL.snapshot()["role"] == "hub"


def test_the_hub_is_told_disabled_rather_than_nothing(as_hub):
    """Every node runs a copy of one database, so a stored `syncServerHost`
    travels with the copy. `disabled` is Trilium's own override for that, and
    without it a hub reseeded from a spoke would sync with itself."""
    env = server.child_env(instances.VAULT)
    assert env["TRILIUM_SYNC_SYNCSERVERHOST"] == "disabled"


def test_a_client_syncs_through_its_own_tunnel(as_client):
    """Never the hub's address. A node can only be pointed at a hub it holds a
    forward to, so there is no way to name a sync target nothing listens on."""
    env = server.child_env(instances.VAULT)
    assert env["TRILIUM_SYNC_SYNCSERVERHOST"] == "http://127.0.0.1:12611"


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


def test_the_forward_refuses_to_be_multiplexed(as_client):
    """A forward carried by another process's master would outlive a stop here
    and could not be signalled by it."""
    cmd = sync.tunnel_cmd()
    assert "ControlMaster=no" in cmd
    assert "ControlPath=none" in cmd


def test_a_hub_has_no_command_to_run(as_hub):
    with pytest.raises(RuntimeError, match="this node is the hub"):
        sync.tunnel_cmd()


def test_the_tunnel_may_not_take_the_vault_s_own_port(as_client, monkeypatch):
    """They are both loopback ports on one machine, and the collision would
    show up as a vault that will not start."""
    monkeypatch.setenv("TRILIUM_SYNC_TUNNEL_PORT", str(instances.UPSTREAM_PORT))
    with pytest.raises(RuntimeError, match="the vault's own port"):
        sync.tunnel_cmd()


def test_the_tunnel_is_inert_on_the_hub(as_hub):
    """Nothing spawned, and the loop that reconciles it says so rather than
    reporting a failure every twenty seconds."""
    tunnel = sync.Tunnel()
    assert tunnel.start()["action"] == "not-a-client"
    assert tunnel.reconcile()["action"] == "not-a-client"
    assert tunnel.snapshot()["running"] is False


def test_a_hub_that_keeps_refusing_is_retried_at_a_widening_interval(
        as_client, monkeypatch):
    """capella sleeps and sirius reboots. A link that cannot open is a normal
    state, so it must not be retried on every tick for as long as it lasts."""
    tunnel = sync.Tunnel()
    monkeypatch.setattr(tunnel, "_spawn", lambda: (_ for _ in ()).throw(
        OSError("connection refused")))
    first = tunnel.reconcile()
    assert first["action"] == "reopen-failed"
    second = tunnel.reconcile()
    assert second["action"] == "waiting"
