"""A declared singleton homed on another node never runs here: discovery holds
it disabled over an explicit ``enabled.json`` entry, and ``services enable`` and
``services start`` refuse it, naming the owner."""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = [pytest.mark.unit]

from awm.gateway import gateway_ops, peer_catalog
from awm.gateway.hub import discovery


@pytest.fixture()
def services_tree(tmp_path, monkeypatch, awm_workspace):
    """A tmp services root with ``2fa``, a ``chat`` folder publishing tool
    ``social``, and a plain ``plain-svc``. The peer book holds domestic ``mira``."""
    root = tmp_path / "services"
    for name, marker in (("2fa", None), ("chat", 'tool = "social"\n'), ("plain-svc", None)):
        d = root / name
        d.mkdir(parents=True)
        (d / "run.sh").write_text("#!/bin/bash\n")
        if marker is not None:
            (d / "service.toml").write_text(marker)
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    monkeypatch.delenv("AWM_TWOFA_PEER", raising=False)
    for key in [k for k in os.environ if k.startswith("AWM_DOMAIN_HOME_")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(peer_catalog, "_domestic_peers", lambda: frozenset({"mira"}))
    return root


def _enabled_map():
    return {s.name: s.enabled for s in discovery.discover_services()}


def test_homed_elsewhere_beats_an_explicit_true(services_tree, monkeypatch):
    monkeypatch.setenv("AWM_TWOFA_PEER", "mira")
    discovery.set_enabled("2fa", True)
    assert discovery.is_enabled("2fa") is False
    assert _enabled_map()["2fa"] is False
    assert discovery.singleton_owner("2fa") == "mira"


def test_home_matches_the_tool_domain_not_the_folder(services_tree, monkeypatch):
    monkeypatch.setenv("AWM_DOMAIN_HOME_social", "mira")
    assert _enabled_map() == {"2fa": True, "chat": False, "plain-svc": True}


def test_no_home_leaves_resolution_unchanged(services_tree):
    discovery.set_enabled("plain-svc", False)
    assert _enabled_map() == {"2fa": True, "chat": True, "plain-svc": False}
    assert discovery.singleton_owner("2fa") is None


def test_home_on_this_node_runs_here(services_tree, monkeypatch):
    # The peer book never lists the node itself, so a home naming this node
    # is not a domestic peer and gates nothing.
    monkeypatch.setenv("AWM_TWOFA_PEER", "capella")
    assert discovery.is_enabled("2fa") is True
    assert discovery.singleton_owner("2fa") is None


def test_enable_and_start_refuse_naming_the_owner(services_tree, monkeypatch):
    monkeypatch.setenv("AWM_TWOFA_PEER", "mira")
    with pytest.raises(FileExistsError, match="homed on mira"):
        asyncio.run(gateway_ops._op_services_enable("2fa"))
    assert "2fa" not in discovery.load_enabled()
    with pytest.raises(FileExistsError, match="homed on mira"):
        asyncio.run(gateway_ops._op_services_start("2fa"))
