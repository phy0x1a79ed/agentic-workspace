"""Unit tests for the penpot-plugins static-mount config and status snapshot.

Mount *registration* against a live gateway is covered by the gateway's own
``kind=static`` tests (``awm/gateway/tests/test_hub_static.py``); here we test
the pieces this service owns: root resolution, the plugin-discovery list in
the status snapshot, and the shape of that snapshot before/after "mounted".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awm.penpot_plugins import mount


# ---- local_root() ----------------------------------------------------------


@pytest.mark.smoke
def test_local_root_defaults_to_sibling_local_dir(monkeypatch):
    monkeypatch.delenv("PENPOT_PLUGINS_ROOT", raising=False)
    root = mount.local_root()
    assert root.name == "local"
    # Sibling of the awm/penpot_plugins/ package dir, i.e. the service root.
    assert root.parent == Path(__file__).resolve().parents[1]


@pytest.mark.smoke
def test_local_root_honours_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PENPOT_PLUGINS_ROOT", str(tmp_path))
    assert mount.local_root() == tmp_path


# ---- the real local/ tree in this repo -------------------------------------


# Every plugin this service ships. Penpot's Plugin Manager fetches manifest.json
# and then the files it names, so a missing one is a 404 mid-install rather than
# a startup error here.
SHIPPED_PLUGINS = ("penpot-view-refresh", "penpot-view-link")


@pytest.mark.smoke
@pytest.mark.parametrize("name", SHIPPED_PLUGINS)
def test_real_local_tree_ships_plugin(name: str):
    root = mount.local_root()
    manifest_path = root / name / "manifest.json"
    assert manifest_path.is_file()

    manifest = json.loads(manifest_path.read_text())
    # version 2 is what makes `code`, `icon` and the penpot.ui.open path
    # resolve relative to wherever the manifest was served from. Without it
    # every asset path has to be absolute and the plugin stops being portable
    # between the local gateway and the edge.
    assert manifest["version"] == 2
    for key in ("code", "icon"):
        assert (root / name / manifest[key]).is_file()


@pytest.mark.smoke
def test_template_is_a_scaffold_not_an_installable_plugin():
    root = mount.local_root()
    assert (root / "_template" / "manifest.json").is_file()
    assert (root / "_template" / "plugin.ts").is_file()
    assert not (root / "_template" / "plugin.js").exists()


@pytest.mark.smoke
def test_status_lists_every_shipped_plugin():
    snap = mount._State().snapshot()
    assert set(SHIPPED_PLUGINS) <= set(snap["plugins"])
    assert "_template" not in snap["plugins"]


# ---- status() snapshot ------------------------------------------------------


@pytest.mark.smoke
def test_status_shape_before_mount(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PENPOT_PLUGINS_ROOT", str(tmp_path))
    fresh = mount._State()
    snap = fresh.snapshot()
    assert snap["mounted"] is False
    assert snap["prefix"] == mount.MOUNT_PREFIX
    assert snap["service_id"] is None
    assert snap["plugins"] == []  # tmp_path is empty


@pytest.mark.smoke
def test_status_lists_only_dirs_with_a_manifest(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PENPOT_PLUGINS_ROOT", str(tmp_path))
    (tmp_path / "real-plugin").mkdir()
    (tmp_path / "real-plugin" / "manifest.json").write_text("{}")
    (tmp_path / "no-manifest").mkdir()  # not a plugin — no manifest.json
    (tmp_path / "_template").mkdir()  # scaffold — leading underscore excluded
    (tmp_path / "_template" / "manifest.json").write_text("{}")
    (tmp_path / "stray.txt").write_text("not a dir")

    st = mount._State()
    snap = st.snapshot()
    assert snap["plugins"] == ["real-plugin"]
    assert snap["install_url_shape"] == f"{mount.MOUNT_PREFIX}/<name>/manifest.json"


@pytest.mark.smoke
def test_status_shape_when_mounted():
    st = mount._State()
    with st.lock:
        st.mounted, st.service_id, st.reason = True, "abc123", "ok"
    snap = st.snapshot()
    assert snap["mounted"] is True
    assert snap["service_id"] == "abc123"
    assert snap["reason"] == "ok"
