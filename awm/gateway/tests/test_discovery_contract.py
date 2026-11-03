"""Discovery refuses a folder that breaks the plugin contract, at load (plugin-contract.md § Manifest)."""

from __future__ import annotations

import pytest

from awm.gateway.hub import discovery


def _plugin(root, name, body, *, run=True):
    d = root / name
    d.mkdir(parents=True)
    if run:
        (d / "run.sh").write_text("#!/bin/bash\n")
    (d / discovery.PLUGIN_MANIFEST).write_text(body)
    return d


def _simple(name, tool=None, extra=""):
    return f"[plugin]\nname = '{name}'\n[[service]]\nname = '{name}'\n[[tool]]\nname = '{tool or name}'\n{extra}"


def _legacy(root, name, tool):
    d = root / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/bash\n")
    (d / discovery.PROFILE_MARKER).write_text(f'tool = "{tool}"\n')


@pytest.fixture()
def roots(tmp_path, monkeypatch, awm_workspace):
    core, other = tmp_path / "services", tmp_path / "other"
    core.mkdir()
    other.mkdir()
    monkeypatch.setenv("AWM_SERVICES_DIR", str(core))
    monkeypatch.setenv("AWM_SERVICE_ROOTS", str(other))
    for key in [k for k in __import__("os").environ if k.startswith("AWM_DOMAIN_HOME_")]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("AWM_TWOFA_PEER", raising=False)
    discovery._logged.clear()
    return core, other


def test_each_rule_refuses_its_folder_and_names_why(roots):
    core, other = roots
    _plugin(core, "provider", _simple("provider", "prov", "major = 2\nimplements = ['harness@1']\n"))
    _plugin(core, "dependent", _simple("dependent", extra="[[needs.tool]]\nname = 'prov'\nmajor = 2\n"))
    _plugin(core, "journal", _simple("journal"))
    _plugin(other, "dup", _simple("dup", "prov"))
    _plugin(other, "takes-2fa", _simple("takes-2fa", "2fa"))
    _plugin(other, "takes-journal", _simple("takes-journal", "journal"))
    _plugin(other, "missing", _simple("missing", extra="[[needs.tool]]\nname = 'nobody'\n"))
    _plugin(other, "wrong-major", _simple("wrong-major", extra="[[needs.tool]]\nname = 'prov'\nmajor = 1\n"))
    _plugin(other, "escape", "[plugin]\nname = 'escape'\n[[service]]\nname = 'escape'\npath = '..'\n")
    _plugin(other, "bad-name", _simple("bad-name", "a_b"))
    _plugin(other, "has-uuid", _simple("has-uuid", extra="uuid = '6f1c2c1e-0d4b-4f3e-9a51-3b8f2d7a9c10'\n"))
    _plugin(other, "bad-toml", "[plugin\nname =")
    _plugin(other, "windows-only", _simple("windows-only") + "[needs]\nplatform = ['windows']\n")
    _plugin(other, "bad-implements", _simple("bad-implements", extra="implements = ['harness']\n"))
    _plugin(other, "needs-refused", _simple("needs-refused", extra="[[needs.tool]]\nname = 'bad-implements'\n"))

    refused = discovery.plugin_refusals()
    assert set(refused) == {"dup", "takes-2fa", "missing", "wrong-major", "escape", "bad-name", "has-uuid",
                            "bad-toml", "windows-only", "bad-implements", "needs-refused", "takes-journal"}
    assert "already served by plugin 'provider'" in refused["dup"]
    assert "reserved" in refused["takes-2fa"]
    assert "'nobody'" in refused["missing"]
    assert "major 1" in refused["wrong-major"]
    assert "leaves the plugin folder" in refused["escape"]
    assert "unknown key 'uuid'" in refused["has-uuid"]
    assert "platform" in refused["windows-only"]
    assert "<contract>@<major>" in refused["bad-implements"]
    names = {s.name for s in discovery.discover_services()}
    assert names == {"provider", "dependent", "journal"}


def test_a_reserved_name_needs_a_core_root_and_own_trust(roots):
    core, _ = roots
    _plugin(core, "twofa", _simple("twofa", "2fa").replace("[plugin]\n", "[plugin]\ntrust = 'third-party'\n"))
    assert "reserved" in discovery.plugin_refusals()["twofa"]


def test_legacy_folders_share_a_tool_but_a_manifest_may_not_join_them(roots):
    core, other = roots
    _legacy(core, "cx", "reflection")
    _legacy(core, "keep", "reflection")
    _plugin(other, "steals", _simple("steals", "reflection"))
    refused = discovery.plugin_refusals()
    assert set(refused) == {"steals"}


def test_a_need_on_a_singleton_homed_on_a_peer_resolves(roots, monkeypatch):
    core, _ = roots
    monkeypatch.setattr("awm.gateway.peer_catalog.declared_homes", lambda: {"2fa": "alcor"})
    _plugin(core, "vpn", _simple("vpn", extra="[[needs.tool]]\nname = '2fa'\n"))
    assert discovery.plugin_refusals() == {}
