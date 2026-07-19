"""Tests for awm.gateway.deploy — the pure planning + signature logic behind
``awm deploy``.

The imperative shell (subprocesses, systemd restart, hub poll) lives in cli.py
and is exercised live; here we pin the decisions: what changes trigger an
install vs a build, the change manifest round-trip, and the verify diff.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

from awm.gateway import deploy


def _make_tree(root, *, services=("stt", "tts"), components=("config", "persistence"),
               pages=None):
    """Build a minimal awm/ tree: gateway + component libs + services (each with
    an install.sh) + buildable pages."""
    (root / "gateway").mkdir(parents=True)
    (root / "gateway" / "install.sh").write_text("#!/bin/bash\n")
    comp = root / "service_components"
    comp.mkdir()
    for c in components:
        (comp / c).mkdir()
    svc = root / "services"
    svc.mkdir()
    for s in services:
        (svc / s).mkdir()
        (svc / s / "install.sh").write_text("#!/bin/bash\n")
    pdir = root / "pages"
    pdir.mkdir()
    for name, files in (pages or {}).items():
        p = pdir / name
        (p / "src").mkdir(parents=True)
        (p / "index.html").write_text(files.get("index", "<html></html>"))
        for rel, content in files.get("src", {}).items():
            (p / "src" / rel).write_text(content)
    return root


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

def test_dist_set_signature_stable(tmp_path):
    root = _make_tree(tmp_path / "a")
    s1 = deploy.dist_set_signature(root)
    s2 = deploy.dist_set_signature(root)
    assert s1 == s2


def test_dist_set_signature_ignores_code_edits(tmp_path):
    # Editable install: a code edit inside a service must NOT change the sig.
    root = _make_tree(tmp_path / "a")
    before = deploy.dist_set_signature(root)
    (root / "services" / "stt" / "handler.py").write_text("print('edit')\n")
    assert deploy.dist_set_signature(root) == before


def test_dist_set_signature_changes_on_new_service(tmp_path):
    root = _make_tree(tmp_path / "a")
    before = deploy.dist_set_signature(root)
    newsvc = root / "services" / "brandnew"
    newsvc.mkdir()
    (newsvc / "install.sh").write_text("#!/bin/bash\n")
    assert deploy.dist_set_signature(root) != before


def test_service_without_install_sh_not_counted(tmp_path):
    root = _make_tree(tmp_path / "a")
    before = deploy.dist_set_signature(root)
    # A service dir with a run.sh but NO install.sh (e.g. tts) is not an
    # installable unit — adding one must not change the dist-set sig.
    d = root / "services" / "runonly"
    d.mkdir()
    (d / "run.sh").write_text("#!/bin/bash\n")
    assert deploy.dist_set_signature(root) == before


def test_page_signature_content_sensitive(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {"src": {"App.svelte": "v1"}}})
    p = root / "pages" / "fleet"
    before = deploy.page_signature(p)
    (p / "src" / "App.svelte").write_text("v2")
    assert deploy.page_signature(p) != before


def test_page_signature_ignores_dist(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {"src": {"App.svelte": "v1"}}})
    p = root / "pages" / "fleet"
    before = deploy.page_signature(p)
    (p / "dist").mkdir()
    (p / "dist" / "index.html").write_text("<html>built</html>")
    # Building the page (writing dist/) must not change its SOURCE signature.
    assert deploy.page_signature(p) == before


def test_page_signatures_only_buildable(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {}})
    # A source-only dir with no index.html is not buildable → not signed.
    (root / "pages" / "placeholder").mkdir()
    sigs = deploy.page_signatures(root)
    assert set(sigs) == {"fleet"}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_manifest_roundtrip(tmp_path):
    assert deploy.load_manifest(tmp_path) == {}
    deploy.save_manifest(tmp_path, {"dist_sig": "abc", "page_sigs": {"fleet": "x"}})
    assert deploy.load_manifest(tmp_path) == {"dist_sig": "abc",
                                              "page_sigs": {"fleet": "x"}}


def test_manifest_corrupt_returns_empty(tmp_path):
    deploy.manifest_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    deploy.manifest_path(tmp_path).write_text("{not json")
    assert deploy.load_manifest(tmp_path) == {}


# ---------------------------------------------------------------------------
# plan_deploy
# ---------------------------------------------------------------------------

def test_plan_no_prior_manifest_installs_and_builds(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {"src": {"a": "1"}}})
    awm_dir = tmp_path / "awm_dir"
    awm_dir.mkdir()
    plan = deploy.plan_deploy(root, awm_dir)
    assert plan.do_install and "no prior" in plan.install_reason
    assert plan.do_build and plan.changed_pages == ["fleet"]
    assert plan.do_reap


def test_plan_unchanged_skips_install_and_build(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {"src": {"a": "1"}}})
    awm_dir = tmp_path / "awm_dir"
    awm_dir.mkdir()
    # Record the current signatures as the last successful deploy.
    first = deploy.plan_deploy(root, awm_dir)
    deploy.save_manifest(awm_dir, first.next_manifest())

    plan = deploy.plan_deploy(root, awm_dir)
    assert not plan.do_install and "unchanged" in plan.install_reason
    assert not plan.do_build and plan.changed_pages == []


def test_plan_new_service_triggers_install_only(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {"src": {"a": "1"}}})
    awm_dir = tmp_path / "awm_dir"
    awm_dir.mkdir()
    deploy.save_manifest(awm_dir, deploy.plan_deploy(root, awm_dir).next_manifest())

    d = root / "services" / "brandnew"
    d.mkdir()
    (d / "install.sh").write_text("#!/bin/bash\n")
    plan = deploy.plan_deploy(root, awm_dir)
    assert plan.do_install and "dist set changed" in plan.install_reason
    assert not plan.do_build  # pages untouched


def test_plan_page_change_triggers_build_only(tmp_path):
    root = _make_tree(tmp_path / "a",
                      pages={"fleet": {"src": {"a": "1"}}, "notes": {"src": {"b": "1"}}})
    awm_dir = tmp_path / "awm_dir"
    awm_dir.mkdir()
    deploy.save_manifest(awm_dir, deploy.plan_deploy(root, awm_dir).next_manifest())

    (root / "pages" / "fleet" / "src" / "a").write_text("2")
    plan = deploy.plan_deploy(root, awm_dir)
    assert not plan.do_install
    assert plan.do_build and plan.changed_pages == ["fleet"]  # only fleet, not notes


def test_plan_force_does_everything(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {"src": {"a": "1"}}})
    awm_dir = tmp_path / "awm_dir"
    awm_dir.mkdir()
    deploy.save_manifest(awm_dir, deploy.plan_deploy(root, awm_dir).next_manifest())

    plan = deploy.plan_deploy(root, awm_dir, force=True)
    assert plan.do_install and "forced" in plan.install_reason
    assert plan.do_build and plan.changed_pages == ["fleet"]


def test_plan_flags_veto(tmp_path):
    root = _make_tree(tmp_path / "a", pages={"fleet": {"src": {"a": "1"}}})
    awm_dir = tmp_path / "awm_dir"
    awm_dir.mkdir()
    plan = deploy.plan_deploy(root, awm_dir, no_install=True, no_build=True,
                              no_reap=True)
    assert not plan.do_install and "--no-install" in plan.install_reason
    assert not plan.do_build and plan.changed_pages == []
    assert not plan.do_reap


# ---------------------------------------------------------------------------
# missing_from_listing (verify diff)
# ---------------------------------------------------------------------------

def test_missing_when_registry_empty():
    m = deploy.missing_from_listing([], ["agents", "tts"], ["fleet"])
    assert m == {"services": ["agents", "tts"], "pages": ["fleet"]}


def test_present_services_and_pages_not_missing():
    listing = [
        {"name": "agents", "backend_status": "ready", "prefix": "/svc/agents"},
        {"name": "tts", "backend_status": "starting", "prefix": "/svc/tts"},
        {"name": "fleet", "backend_status": "ready", "prefix": "/ui/fleet",
         "kind": "page"},
    ]
    m = deploy.missing_from_listing(listing, ["agents", "tts"], ["fleet"])
    assert m == {"services": [], "pages": []}


def test_service_present_but_dead_status_counts_missing():
    listing = [{"name": "agents", "backend_status": "dead", "prefix": "/svc/agents"}]
    m = deploy.missing_from_listing(listing, ["agents"], [])
    assert m == {"services": ["agents"], "pages": []}
