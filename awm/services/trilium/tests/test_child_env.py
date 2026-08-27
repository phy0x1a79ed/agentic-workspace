"""The invariant the vault's missing password rests on.

Trilium runs here with its own authentication off. That is only safe while an
awm edge listener is the *only* way to reach the child, so the two facts that
make it true are asserted rather than documented: the bind is loopback, and
nothing in the surrounding environment can move it.
"""

from __future__ import annotations

import pytest

from awm import config
from awm.trilium import instances, server

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_authentication_is_off_by_default():
    """There is one login, and it is awm's. A second would ask the same
    question twice — and with one shared vault it could not even answer 'which
    person', which is what it used to be for."""
    env = server.child_env(instances.VAULT)
    assert env["TRILIUM_GENERAL_NOAUTHENTICATION"] == "true"


def test_turning_the_invariant_off_restores_trilium_s_own_login(monkeypatch):
    """`TRILIUM_EDGE_ONLY=0` is the supported way to reach the vault by some
    other route, and it must take the password back with it. One knob, so
    nobody can set half of this."""
    monkeypatch.setattr(instances, "EDGE_ONLY", False)
    env = server.child_env(instances.VAULT)
    assert "TRILIUM_GENERAL_NOAUTHENTICATION" not in env


def test_the_bind_is_loopback_whatever_the_environment_says(monkeypatch):
    """Both levers, because the cost of losing this one is a public,
    unauthenticated knowledge base.

    `TRILIUM_HOST` beats `Network.host` in the fork today, but that ordering is
    upstream's to change — and `Network.host` *defaults to 0.0.0.0*. So the
    losing lever is removed from the child's environment rather than merely
    out-ranked.
    """
    monkeypatch.setenv("TRILIUM_HOST", "0.0.0.0")
    monkeypatch.setenv("TRILIUM_NETWORK_HOST", "0.0.0.0")
    env = server.child_env(instances.VAULT)
    assert env["TRILIUM_HOST"] == "127.0.0.1"
    assert "TRILIUM_NETWORK_HOST" not in env


def test_the_port_comes_from_the_one_place_that_defines_it():
    """The supervisor binds it and the edge proxies to it. Two copies of this
    number is a vault nobody can reach and no error that says why."""
    env = server.child_env(instances.VAULT)
    assert env["TRILIUM_PORT"] == str(config.VAULT_PORT)
    assert instances.UPSTREAM_PORT == config.VAULT_PORT


def test_arbitrary_code_execution_stays_off():
    """Both default off on a server build. Set anyway, because a config.ini in
    the data directory can turn either on and the vault is public."""
    env = server.child_env(instances.VAULT)
    assert env["TRILIUM_SECURITY_BACKEND_SCRIPTING_ENABLED"] == "false"
    assert env["TRILIUM_SECURITY_SQL_CONSOLE_ENABLED"] == "false"
