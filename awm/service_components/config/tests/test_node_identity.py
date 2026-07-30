"""Node identity — the fleet name and edge URL a node puts in shared messages.

The bug these pin: ``socket.gethostname()`` was the only node-identity in the
tree, and mira's hostname is ``pavilion``, so its SSH lockout alerts named a
machine nobody in the fleet calls mira. With three nodes pushing passwords into
one Discord channel, an unnamed message is unusable.

The edge URL is env-first on purpose. capella's edge is reached at the *Windows*
host's mesh address, which is invisible from inside WSL — so enumeration is the
fallback, never the authority.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("AWM_NODE_NAME", "AWM_EDGE_URL", "AWM_MESH_SUBNET",
                "AWM_HTTPS_PORT"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# node_name
# ---------------------------------------------------------------------------

def test_node_name_prefers_the_declared_name(monkeypatch):
    from awm import config
    monkeypatch.setenv("AWM_NODE_NAME", "mira")
    monkeypatch.setattr("socket.gethostname", lambda: "pavilion")
    assert config.node_name() == "mira"


def test_node_name_falls_back_to_hostname(monkeypatch):
    from awm import config
    monkeypatch.setattr("socket.gethostname", lambda: "pavilion")
    assert config.node_name() == "pavilion"


@pytest.mark.parametrize("declared", ["", "   "])
def test_node_name_treats_blank_as_undeclared(monkeypatch, declared):
    """An env file line left empty must not name the node the empty string."""
    from awm import config
    monkeypatch.setenv("AWM_NODE_NAME", declared)
    monkeypatch.setattr("socket.gethostname", lambda: "pavilion")
    assert config.node_name() == "pavilion"


def test_node_name_strips_surrounding_whitespace(monkeypatch):
    from awm import config
    monkeypatch.setenv("AWM_NODE_NAME", " altair \n")
    assert config.node_name() == "altair"


# ---------------------------------------------------------------------------
# mesh_address
# ---------------------------------------------------------------------------

def _fake_hostname_I(monkeypatch, stdout: str):
    """Stub the one subprocess mesh_address shells out to."""
    import subprocess
    from types import SimpleNamespace

    def _run(argv, **kw):
        assert argv[:2] == ["hostname", "-I"]
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", _run)


def test_mesh_address_picks_the_address_inside_the_mesh(monkeypatch):
    from awm import config
    _fake_hostname_I(monkeypatch, "192.168.100.142 10.74.81.213 172.17.0.1\n")
    assert config.mesh_address() == "10.74.81.213"


def test_mesh_address_is_none_off_the_mesh(monkeypatch):
    """A node reached through another host's ZeroTier membership has none."""
    from awm import config
    _fake_hostname_I(monkeypatch, "172.16.0.22 172.17.0.1\n")
    assert config.mesh_address() is None


def test_mesh_address_honours_a_declared_subnet(monkeypatch):
    from awm import config
    monkeypatch.setenv("AWM_MESH_SUBNET", "10.147.0.0/16")
    _fake_hostname_I(monkeypatch, "10.74.81.213 10.147.19.4\n")
    assert config.mesh_address() == "10.147.19.4"


def test_mesh_address_survives_a_bad_subnet(monkeypatch):
    from awm import config
    monkeypatch.setenv("AWM_MESH_SUBNET", "not-a-subnet")
    _fake_hostname_I(monkeypatch, "10.74.81.213\n")
    assert config.mesh_address() is None


def test_mesh_address_survives_hostname_blowing_up(monkeypatch):
    """No `hostname` binary, or it hung — absent, not fatal."""
    import subprocess
    from awm import config

    def _boom(*a, **k):
        raise OSError("no hostname binary")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert config.mesh_address() is None


def test_mesh_address_ignores_junk_tokens(monkeypatch):
    from awm import config
    _fake_hostname_I(monkeypatch, "fe80::1%eth0 not-an-ip 10.74.81.213\n")
    assert config.mesh_address() == "10.74.81.213"


# ---------------------------------------------------------------------------
# edge_url
# ---------------------------------------------------------------------------

def test_edge_url_prefers_the_declared_url(monkeypatch):
    from awm import config
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.110:12100")
    _fake_hostname_I(monkeypatch, "10.74.81.213\n")   # would win if enumerated
    assert config.edge_url() == "https://10.74.81.110:12100"


def test_edge_url_strips_a_trailing_slash(monkeypatch):
    """Callers append `/__auth/link`; a double slash would 404."""
    from awm import config
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.111:12100/")
    assert config.edge_url() == "https://10.74.81.111:12100"


def test_edge_url_falls_back_to_the_enumerated_mesh_address(monkeypatch):
    from awm import config
    monkeypatch.setenv("AWM_HTTPS_PORT", "12100")
    _fake_hostname_I(monkeypatch, "192.168.100.142 10.74.81.213\n")
    assert config.edge_url() == "https://10.74.81.213:12100"


def test_edge_url_uses_the_default_port_when_unset(monkeypatch):
    from awm import config
    _fake_hostname_I(monkeypatch, "10.74.81.213\n")
    assert config.edge_url() == "https://10.74.81.213:8443"


def test_edge_url_is_none_when_undeclared_and_unenumerable(monkeypatch):
    """Better no link than a link that goes nowhere — the caller omits it."""
    from awm import config
    _fake_hostname_I(monkeypatch, "172.16.0.22\n")
    assert config.edge_url() is None
