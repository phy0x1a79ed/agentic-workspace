"""A page is switched off by its own ``enabled.json`` entry or by its same-named
service, and ``services enable|disable`` toggles the live ``/ui/<name>`` base."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.unit]

from awm.gateway.hub import discovery


@pytest.fixture()
def tree(awm_workspace, tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    services = tmp_path / "svcs"
    for name in ("solo", "paired"):
        (pages / name / "dist").mkdir(parents=True)
    (services / "paired").mkdir(parents=True)
    (services / "paired" / "run.sh").write_text("exit 0\n", encoding="utf-8")
    monkeypatch.setenv("AWM_PAGES_DIR", str(pages))
    monkeypatch.setenv("AWM_SERVICES_DIR", str(services))
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    return pages


def _names(**kw) -> set[str]:
    return {s.name for s in discovery.discover_pages(**kw)}


def test_pages_enabled_by_default(tree):
    assert _names() == {"solo", "paired"}


def test_explicit_entry_disables_page(tree):
    discovery.set_enabled("solo", False)
    assert _names() == {"paired"}
    assert _names(include_disabled=True) == {"solo", "paired"}


def test_page_follows_disabled_service(tree):
    discovery.set_enabled("paired", False)
    assert _names() == {"solo"}


def test_page_follows_service_profile_gate(tree, monkeypatch):
    marker = discovery.services_root() / "paired" / "service.toml"
    marker.write_text('profiles = ["x"]\n', encoding="utf-8")
    assert _names() == {"solo"}
    monkeypatch.setenv("AWM_PROFILES", "x")
    assert _names() == {"solo", "paired"}


def test_disable_and_enable_toggle_live_page(tree):
    from awm.gateway import gateway_ops
    from awm.gateway.hub.registry import get_registry

    registry = get_registry()

    async def run():
        await registry.register_page("solo", "/ui/solo", str(tree / "solo" / "dist"))
        out = await gateway_ops._op_services_disable("solo")
        assert out["page"] == "/ui/solo"
        assert registry.get_by_name("page", "solo") is None
        assert "solo" not in _names()

        out = await gateway_ops._op_services_enable("solo")
        assert out["page"] == "/ui/solo"
        assert registry.get_by_name("page", "solo") is not None
        await registry.evict_by_name("solo", kind="page")

    asyncio.run(run())


def test_enable_disable_refuse_unknown_name(tree):
    from awm.gateway import gateway_ops

    with pytest.raises(FileNotFoundError):
        asyncio.run(gateway_ops._op_services_disable("nope"))
    assert "nope" not in discovery.load_enabled()
