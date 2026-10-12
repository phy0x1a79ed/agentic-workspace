"""The one-service-one-tool registration rule, applied to the tree and to a live
``ready`` frame."""

from __future__ import annotations

import pytest

from awm.gateway import surface
from awm.gateway.hub import discovery
from awm.gateway.hub.registry import ServiceRecord

from .test_service_manifest_effects import MANIFESTS, SERVICES

pytestmark = [pytest.mark.unit]


def _fn(name, tool=None):
    return {"name": name, **({"tool": tool} if tool else {})}


def _folder(root, name, toml=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/bash\n")
    if toml is not None:
        (d / "service.toml").write_text(toml)
    return d


@pytest.fixture()
def services_root(tmp_path, monkeypatch):
    root = tmp_path / "services"
    root.mkdir()
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    return root


def _violations(allowlist):
    bad = {}
    for service, entries in MANIFESTS.items():
        if service in allowlist:
            continue
        tool = discovery.read_tool(SERVICES / service)
        fns = [{"name": e["name"], **({"tool": e["tool"]} if e.get("tool") else {})}
               for e in entries if isinstance(e["name"], str)]
        found = surface.tool_violations(service, tool, fns)
        if found:
            bad[service] = found
    return bad


def test_every_manifest_in_the_tree_follows_the_rule():
    assert _violations(surface.LEGACY_ALLOWLIST) == {}


def test_the_allowlist_names_real_service_folders():
    missing = {n for n in surface.LEGACY_ALLOWLIST if not (SERVICES / n).is_dir()}
    assert not missing


def test_a_function_outside_its_tool_is_named_in_the_refusal():
    bad = surface.tool_violations("ssh", "ssh", [_fn("connect"), _fn("x", "scope_x")])
    assert len(bad) == 1 and "scope_x" in bad[0] and "'x'" in bad[0]


def test_a_bare_tool_name_is_not_a_verb():
    assert surface.tool_violations("ssh", "ssh", [_fn("x", "ssh_")])
    assert surface.tool_violations("ssh", "ssh", [_fn("x", "ssh")])


def test_the_frozen_identity_rpcs_are_exempt():
    assert not surface.tool_violations("svc", "svc", [_fn("a", "resolveScope")])


def test_tool_names_may_not_hold_an_underscore():
    assert surface.tool_violations("svc", "a_b", [_fn("x", "a_b_x")])


def test_published_name_prefers_the_tool_key():
    assert surface.published_name("ssh", _fn("connect")) == "ssh_connect"
    assert surface.published_name("ssh", _fn("connect", "net_go")) == "net_go"


def test_check_registration_refuses_a_misnamed_function(services_root):
    _folder(services_root, "newsvc", 'tool = "news"\n')
    api = {"functions": [_fn("get", "news_get"), _fn("odd", "other_odd")]}
    with pytest.raises(surface.RegistrationError, match="other_odd"):
        surface.check_registration("newsvc", api, [])


def test_the_tool_defaults_to_the_folder_name(services_root):
    _folder(services_root, "newsvc")
    surface.check_registration("newsvc", {"functions": [_fn("g", "newsvc_get")]}, [])
    with pytest.raises(surface.RegistrationError):
        surface.check_registration("newsvc", {"functions": [_fn("g", "news_get")]}, [])


def test_two_services_may_share_a_tool(services_root):
    _folder(services_root, "a", 'tool = "shared"\n')
    _folder(services_root, "b", 'tool = "shared"\n')
    other = ("a", {"functions": [_fn("x", "shared_x")]})
    surface.check_registration("b", {"functions": [_fn("y", "shared_y")]}, [other])


def test_a_duplicate_verb_in_one_tool_is_refused(services_root):
    _folder(services_root, "a", 'tool = "shared"\n')
    _folder(services_root, "b", 'tool = "shared"\n')
    other = ("a", {"functions": [_fn("x", "shared_x")]})
    with pytest.raises(surface.RegistrationError, match="shared_x.*held by service 'a'"):
        surface.check_registration("b", {"functions": [_fn("x2", "shared_x")]}, [other])


def test_the_same_verb_in_different_tools_is_fine(services_root):
    _folder(services_root, "a", 'tool = "one"\n')
    _folder(services_root, "b", 'tool = "two"\n')
    other = ("a", {"functions": [_fn("x", "one_x")]})
    surface.check_registration("b", {"functions": [_fn("x", "two_x")]}, [other])


def test_a_respawn_does_not_clash_with_itself(services_root):
    _folder(services_root, "a", 'tool = "one"\n')
    api = {"functions": [_fn("x", "one_x")]}
    surface.check_registration("a", api, [("a", api)])


def test_the_allowlist_is_empty():
    assert surface.LEGACY_ALLOWLIST == frozenset()


def test_a_legacy_service_is_not_checked(services_root, monkeypatch):
    monkeypatch.setattr(surface, "LEGACY_ALLOWLIST", frozenset({"oldsvc"}))
    surface.check_registration("oldsvc", {"functions": [_fn("x", "whatever_x")]}, [])


def test_a_legacy_service_keeps_the_prefix_projection(services_root, monkeypatch):
    monkeypatch.setattr(surface, "LEGACY_ALLOWLIST", frozenset({"oldsvc"}))
    assert surface.split_published("oldsvc", "old_create") == ("old", "create")
    _folder(services_root, "newsvc", 'tool = "news"\n')
    assert surface.split_published("newsvc", "news_get_all") == ("news", "get_all")


# -- the ready frame ----------------------------------------------------------


def test_a_refused_manifest_registers_nothing(services_root, monkeypatch):
    from awm.gateway.api import hub
    from awm.gateway.hub import registry

    _folder(services_root, "newsvc", 'tool = "news"\n')
    reg = registry.Registry()
    rec = ServiceRecord(name="newsvc", prefix="/svc/newsvc", kind="service")
    reg._install_base(rec)
    monkeypatch.setattr(hub, "get_registry", lambda: reg)
    ch = hub.rpc.ensure_control(rec.service_id)
    try:
        hub._route_inbound(ch, rec.service_id, {"kind": "ready", "api": {
            "functions": [_fn("get", "news_get"), _fn("odd", "other_odd")]}})
        assert rec.api == {} and rec.backend_status == "refused"
        assert "other_odd" in rec.refusal
        hub._route_inbound(ch, rec.service_id, {"kind": "ready", "api": {
            "functions": [_fn("get", "news_get")]}})
        assert rec.backend_status == "ready" and rec.refusal == ""
        assert rec.api["functions"][0]["name"] == "get"
    finally:
        hub.rpc.drop_control(rec.service_id)
