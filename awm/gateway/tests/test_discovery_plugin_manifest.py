"""Discovery reads a folder's ``awm-plugin.toml`` the way it reads ``service.toml``."""

from __future__ import annotations

import pytest

from awm.gateway.hub import discovery

MANIFEST = """
[plugin]
name = "{name}"
version = "0.1.0"
trust = "own"

[[service]]
name = "{name}"
{service_extra}

[[tool]]
name = "{tool}"
major = 1
tier = "{tier}"
"""


def _plugin(root, name, *, tool=None, tier="on", service_extra="", legacy=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/bash\n")
    (d / discovery.PLUGIN_MANIFEST).write_text(MANIFEST.format(
        name=name, tool=tool or name, tier=tier, service_extra=service_extra))
    if legacy is not None:
        (d / discovery.PROFILE_MARKER).write_text(legacy)
    return d


@pytest.fixture()
def root(tmp_path, monkeypatch, awm_workspace):
    services = tmp_path / "services"
    services.mkdir()
    monkeypatch.setenv("AWM_SERVICES_DIR", str(services))
    monkeypatch.delenv("AWM_SERVICE_ROOTS", raising=False)
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    return services


def test_tool_and_tier_come_from_the_services_tool(root):
    d = _plugin(root, "scopes", tool="scope", tier="off")
    assert discovery.read_tool(d) == "scope"
    assert discovery.read_tier(d) == discovery.TIER_OFF


def test_profiles_gate_from_the_service_entry(root, monkeypatch):
    _plugin(root, "lab", service_extra='profiles = ["gamebot"]')
    assert not discovery.is_enabled("lab")
    monkeypatch.setenv("AWM_PROFILES", "gamebot")
    assert discovery.is_enabled("lab")


def test_archive_from_the_manifest_wins_and_service_toml_fills_the_rest(root):
    d = _plugin(root, "journal", service_extra='\n[[service.archive]]\nname = "journals"\nmode = "index"\n')
    assert [t["name"] for t in discovery.read_archive(d)] == ["journals"]
    e = _plugin(root, "kb", legacy='[[archive]]\nname = "kb"\nmode = "index"\n')
    assert [t["name"] for t in discovery.read_archive(e)] == ["kb"]
    assert discovery.read_tool(e) == "kb"


def test_a_broken_manifest_never_switches_a_tool_off_but_gates_profiles_off(root):
    d = root / "broken"
    d.mkdir()
    (d / "run.sh").write_text("#!/bin/bash\n")
    (d / discovery.PLUGIN_MANIFEST).write_text("[plugin\nname = ")
    assert discovery.read_tier(d) == discovery.TIER_ON
    assert discovery.read_tool(d) == "broken"
    assert not discovery.is_enabled("broken")


def test_a_manifest_without_profiles_runs_everywhere(root):
    _plugin(root, "mic", tier="off")
    assert discovery.is_enabled("mic")
    assert [s.name for s in discovery.discover_services()] == ["mic"]


def test_a_plugin_loads_from_a_root_outside_the_tree(tmp_path, root, monkeypatch):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _plugin(outside, "rlm-factorio", tier="on")
    monkeypatch.setenv("AWM_SERVICE_ROOTS", str(outside))
    spec = discovery.discover_service("rlm-factorio")
    assert spec is not None and spec.enabled
    assert spec.cwd == str(outside / "rlm-factorio")
    assert discovery.service_tool("rlm-factorio") == "rlm-factorio"
