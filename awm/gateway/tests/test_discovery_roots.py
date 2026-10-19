"""Extra service and page roots (``AWM_SERVICE_ROOTS`` / ``AWM_PAGE_ROOTS``)."""

from __future__ import annotations

import logging
import os

import pytest

from awm.gateway import deploy
from awm.gateway.hub import discovery


def _service(root, name, manifest=None, install=False):
    d = root / name
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/bash\n")
    if manifest is not None:
        (d / "service.toml").write_text(manifest)
    if install:
        (d / "install.sh").write_text("#!/bin/bash\n")
    return d


def _page(root, name, built=True):
    d = root / name
    d.mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html>")
    if built:
        (d / "dist").mkdir()
        (d / "dist" / "index.html").write_text("<!doctype html>")
    return d


@pytest.fixture()
def roots(tmp_path, monkeypatch, awm_workspace):
    builtin = tmp_path / "builtin"
    extra = tmp_path / "extra" / "services"
    builtin.mkdir()
    extra.mkdir(parents=True)
    monkeypatch.setenv("AWM_SERVICES_DIR", str(builtin))
    monkeypatch.setenv("AWM_SERVICE_ROOTS", str(extra))
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    return builtin, extra


def test_a_service_in_an_extra_root_is_discovered(roots):
    builtin, extra = roots
    _service(builtin, "core")
    folder = _service(extra, "myproj", 'tool = "mine"\ntier = "off"\n')
    specs = {s.name: s for s in discovery.discover_services()}
    assert set(specs) == {"core", "myproj"}
    assert specs["myproj"].cwd == str(folder)
    assert specs["myproj"].enabled is True
    assert discovery.service_folder("myproj") == folder
    assert discovery.service_tool("myproj") == "mine"
    assert discovery.service_tier("myproj") == discovery.TIER_OFF


def test_the_profile_gate_applies_in_an_extra_root(roots, monkeypatch):
    _, extra = roots
    _service(extra, "myproj", 'profiles = ["myproj"]\n')
    assert discovery.is_enabled("myproj") is False
    monkeypatch.setenv("AWM_PROFILES", "myproj")
    assert discovery.is_enabled("myproj") is True
    assert [s.enabled for s in discovery.discover_services()] == [True]


def test_the_builtin_root_wins_a_clash(roots, caplog):
    builtin, extra = roots
    mine = _service(builtin, "clash", 'tool = "builtin"\n')
    _service(extra, "clash", 'tool = "extra"\n')
    with caplog.at_level(logging.WARNING, logger="awm.hub.discovery"):
        specs = discovery.discover_services()
    assert [(s.name, s.cwd) for s in specs] == [("clash", str(mine))]
    assert discovery.service_tool("clash") == "builtin"
    assert "shadowed" in caplog.text


def test_roots_are_searched_in_order(roots, tmp_path, monkeypatch):
    _, extra = roots
    second = tmp_path / "second"
    _service(extra, "dup", 'tool = "first"\n')
    _service(second, "dup", 'tool = "second"\n')
    _service(second, "only2")
    monkeypatch.setenv("AWM_SERVICE_ROOTS", os.pathsep.join([str(extra), str(second)]))
    assert discovery.service_tool("dup") == "first"
    assert [s.name for s in discovery.discover_services()] == ["dup", "only2"]


def test_a_missing_root_is_ignored_and_logged(roots, tmp_path, monkeypatch, caplog):
    _, extra = roots
    _service(extra, "myproj")
    missing = tmp_path / "nowhere"
    monkeypatch.setenv("AWM_SERVICE_ROOTS",
                       os.pathsep.join(["", str(missing), str(extra)]))
    with caplog.at_level(logging.WARNING, logger="awm.hub.discovery"):
        specs = discovery.discover_services()
    assert [s.name for s in specs] == ["myproj"]
    assert str(missing) in caplog.text


def test_no_extra_roots_changes_nothing(roots, monkeypatch):
    builtin, _ = roots
    _service(builtin, "core")
    monkeypatch.delenv("AWM_SERVICE_ROOTS")
    assert discovery.service_roots() == [builtin.resolve()]
    assert [s.name for s in discovery.discover_services()] == ["core"]
    assert discovery.service_folder("absent") is None
    assert discovery.service_tool("absent") == "absent"


def test_archive_sources_include_an_extra_root(roots):
    _, extra = roots
    _service(extra, "myproj", '[[archive]]\nname = "mine"\nmode = "index"\n')
    assert discovery.archive_sources() == ["mine"]
    assert discovery.service_archive("myproj")[0]["name"] == "mine"


def test_a_page_in_an_extra_root_is_discovered(tmp_path, monkeypatch, awm_workspace):
    builtin = tmp_path / "pages"
    extra = tmp_path / "extra" / "pages"
    _page(builtin, "core")
    _page(builtin, "clash")
    _page(extra, "clash")
    page = _page(extra, "myproj")
    _page(extra, "unbuilt", built=False)
    monkeypatch.setenv("AWM_PAGES_DIR", str(builtin))
    monkeypatch.setenv("AWM_PAGE_ROOTS",
                       os.pathsep.join([str(tmp_path / "nowhere"), str(extra)]))
    specs = {p.name: p for p in discovery.discover_pages()}
    assert set(specs) == {"core", "clash", "myproj"}
    assert specs["myproj"].dist_dir == str(page / "dist")
    assert specs["myproj"].prefix == "/ui/myproj"
    assert specs["clash"].dist_dir == str(builtin.resolve() / "clash" / "dist")


def test_a_page_follows_its_service_in_an_extra_root(roots, tmp_path, monkeypatch):
    _, extra = roots
    _service(extra, "myproj", 'profiles = ["myproj"]\n')
    pages = tmp_path / "extra" / "pages"
    _page(pages, "myproj")
    monkeypatch.setenv("AWM_PAGE_ROOTS", str(pages))
    assert discovery.discover_pages() == []
    monkeypatch.setenv("AWM_PROFILES", "myproj")
    assert [p.name for p in discovery.discover_pages()] == ["myproj"]


def test_deploy_installs_and_builds_extra_roots(tmp_path, monkeypatch):
    root = tmp_path / "awm"
    _service(root / "services", "core", install=True)
    _page(root / "pages", "core", built=False)
    extra_svc = tmp_path / "extra" / "services"
    extra_pages = tmp_path / "extra" / "pages"
    _service(extra_svc, "myproj", install=True)
    _service(extra_svc, "core", install=True)  # shadowed by the built-in one
    _page(extra_pages, "myproj", built=False)
    assert deploy.installable_units(root) == ["gateway", "service:core"]
    monkeypatch.setenv("AWM_SERVICE_ROOTS", str(extra_svc))
    monkeypatch.setenv("AWM_PAGE_ROOTS", str(extra_pages))
    assert deploy.installable_units(root) == ["gateway", "service:core",
                                              "service:myproj"]
    assert set(deploy.page_signatures(root)) == {"core", "myproj"}


def test_deploy_installs_a_service_that_moved_to_another_root(tmp_path, monkeypatch):
    root = tmp_path / "awm"
    _service(root / "services", "core", install=True)
    first = tmp_path / "first" / "services"
    second = tmp_path / "second" / "services"
    _service(first, "myproj", install=True)
    second.mkdir(parents=True)
    monkeypatch.setenv("AWM_SERVICE_ROOTS", f"{first}{os.pathsep}{second}")
    awm_dir = tmp_path / "awm_dir"
    deploy.save_manifest(awm_dir, deploy.plan_deploy(root, awm_dir).next_manifest())
    assert not deploy.plan_deploy(root, awm_dir).do_install

    # Same name, same install inputs, new folder: the name set is unchanged.
    (first / "myproj").rename(second / "myproj")
    plan = deploy.plan_deploy(root, awm_dir)
    assert plan.dist_sig == deploy.load_manifest(awm_dir)["dist_sig"]
    assert plan.do_install and plan.install_reason == "moved: myproj"
    assert plan.services["myproj"]["path"] == str(second / "myproj")
