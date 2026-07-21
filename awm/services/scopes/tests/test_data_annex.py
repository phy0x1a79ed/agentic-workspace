"""Tests for the git-annex data layer.

Split in two. The first half runs everywhere and pins the *fallback* contract —
a project that hasn't been converted, or a machine without git-annex, must
behave exactly as it did before this layer existed. The second half needs a
real git-annex and proves the properties the layer exists for: isolation,
dedup, transactional merges, safe promotion, safe teardown.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awm.scopes import data_annex as da


pytestmark = pytest.mark.usefixtures("scopes_workspace")

needs_annex = pytest.mark.skipif(
    not da.annex_available(), reason="git-annex not installed",
)


def _sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=da._env())


def _awm_dir(scopes_workspace, project: str, scope: str) -> Path:
    d = scopes_workspace["projects_dir"] / project / scope / ".awm"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Fallback contract — must hold on any machine
# ---------------------------------------------------------------------------

class TestFallback:
    def test_unconverted_project_still_gets_the_shared_symlink(self, scopes_workspace):
        awm = _awm_dir(scopes_workspace, "p", "s")
        rep = da.provision_scope_data("p", "s", awm)
        assert rep["mode"] == "symlink"
        link = awm / "data"
        assert link.is_symlink()
        assert Path(os.readlink(link)) == scopes_workspace["data_dir"] / "p"

    def test_symlink_target_is_created(self, scopes_workspace):
        awm = _awm_dir(scopes_workspace, "p", "s")
        da.provision_scope_data("p", "s", awm)
        assert (scopes_workspace["data_dir"] / "p").is_dir()

    def test_idempotent(self, scopes_workspace):
        awm = _awm_dir(scopes_workspace, "p", "s")
        da.provision_scope_data("p", "s", awm)
        rep = da.provision_scope_data("p", "s", awm)
        assert rep["mode"] == "symlink"
        assert (awm / "data").is_symlink()

    def test_kill_switch_forces_symlink_mode(self, scopes_workspace, monkeypatch):
        monkeypatch.setenv("AWM_DATA_ANNEX", "0")
        assert not da.enabled()
        assert not da.is_annex_project("p")

    def test_refuses_to_replace_an_unrecognised_directory(self, scopes_workspace):
        awm = _awm_dir(scopes_workspace, "p", "s")
        real = awm / "data"
        real.mkdir()
        (real / "someones-work.txt").write_text("do not delete me")
        rep = da.provision_scope_data("p", "s", awm)
        assert rep["mode"] == "unknown"
        assert (real / "someones-work.txt").read_text() == "do not delete me"

    def test_teardown_guard_is_a_noop_without_a_clone(self, scopes_workspace):
        wt = scopes_workspace["projects_dir"] / "p" / "s"
        (wt / ".awm").mkdir(parents=True)
        assert da.prepare_teardown(wt)["result"] == "n/a"

    def test_status_reports_symlink_mode(self, scopes_workspace):
        awm = _awm_dir(scopes_workspace, "p", "s")
        da.provision_scope_data("p", "s", awm)
        st = da.data_status("p", "s", awm.parent)
        assert st["mode"] == "symlink"


class TestManifest:
    """The manifest is what generates the MCP/CLI/HTTP surfaces — guard it."""

    def test_every_function_has_a_handler(self):
        from awm.scopes.hub_adapter import API_MANIFEST, HANDLERS
        names = [f["name"] for f in API_MANIFEST["functions"]]
        assert [n for n in names if n not in HANDLERS] == []
        assert [h for h in HANDLERS if h not in names] == []

    def test_data_verbs_are_projected(self):
        from awm.scopes.hub_adapter import API_MANIFEST
        tools = {f.get("tool", f["name"]) for f in API_MANIFEST["functions"]}
        assert {"scope_heal", "scope_data_status", "scope_data_snapshot",
                "scope_data_promote", "project_data_init"} <= tools

    def test_params_are_well_formed(self):
        """Every param needs a name and a type, and names must be unique —
        the CLI/HTTP/MCP surfaces are all generated from these."""
        from awm.scopes.hub_adapter import API_MANIFEST
        for fn in API_MANIFEST["functions"]:
            names = [p["name"] for p in fn["params"]]
            assert len(names) == len(set(names)), fn["name"]
            assert all(p.get("type") for p in fn["params"]), fn["name"]


class TestBinaryResolution:
    def test_explicit_override_wins(self, tmp_path, monkeypatch):
        fake = tmp_path / "git-annex"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setenv("AWM_ANNEX_BIN", str(fake))
        da.annex_bin.cache_clear()
        try:
            assert da.annex_bin() == str(fake)
        finally:
            da.annex_bin.cache_clear()

    def test_missing_binary_degrades_instead_of_raising(self, scopes_workspace, monkeypatch):
        monkeypatch.setattr(da, "annex_available", lambda: False)
        monkeypatch.setattr(da, "is_annex_project", lambda _p: True)
        awm = _awm_dir(scopes_workspace, "p", "s")
        rep = da.provision_scope_data("p", "s", awm)
        assert rep["mode"] == "symlink"
        assert "git-annex not found" in rep["detail"]


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------

@needs_annex
class TestAnnexLifecycle:
    @pytest.fixture()
    def project(self, scopes_workspace):
        """A converted project with one bulk file, one small file, one secret."""
        data = scopes_workspace["data_dir"] / "demo"
        (data / "raw").mkdir(parents=True)
        (data / "secrets").mkdir(parents=True)
        (data / "raw" / "bulk.bin").write_bytes(os.urandom(300_000))
        (data / "raw" / "notes.md").write_text("# small\n")
        (data / "secrets" / "api_key").write_text("never-annex-me\n")
        (data / ".env").write_text("TOKEN=never-annex-me\n")
        rep = da.init_project_data("demo")
        assert rep["result"] == "converted", rep
        return data

    def test_conversion_routes_by_size_and_excludes_secrets(self, project):
        tracked = _sh("git", "-C", str(project), "ls-files").stdout.split()
        assert "raw/bulk.bin" in tracked
        assert "raw/notes.md" in tracked
        assert not [t for t in tracked if "secret" in t or t == ".env"], tracked
        # Bulk becomes a content-store symlink; small text stays a real file.
        assert (project / "raw" / "bulk.bin").is_symlink()
        assert not (project / "raw" / "notes.md").is_symlink()
        # The secret is untouched on local disk — excluded, not deleted.
        assert (project / "secrets" / "api_key").read_text() == "never-annex-me\n"

    def test_init_is_idempotent(self, project):
        assert da.init_project_data("demo")["result"] in ("up_to_date", "refreshed")

    def test_vendored_checkouts_are_pinned_not_annexed(self, scopes_workspace):
        """A tree of git repos must never be annexed — pin it instead."""
        data = scopes_workspace["data_dir"] / "vend"
        vendor = data / "lib" / "thirdparty"
        vendor.mkdir(parents=True)
        (vendor / "main.py").write_text("print('hi')\n")
        _sh("git", "-C", str(vendor), "init", "-q")
        _sh("git", "-C", str(vendor), "remote", "add", "origin",
            "https://example.invalid/thirdparty.git")
        _sh("git", "-C", str(vendor), "add", "-A")
        _sh("git", "-C", str(vendor), "commit", "-q", "-m", "v1")
        (data / "real.bin").write_bytes(os.urandom(200_000))

        rep = da.init_project_data("vend")
        assert rep["result"] == "converted", rep
        assert rep["vendored"] == 1

        tracked = _sh("git", "-C", str(data), "ls-files").stdout.split()
        assert not [t for t in tracked if t.startswith("lib/")], tracked
        assert "VENDORED.tsv" in tracked
        manifest = (data / "VENDORED.tsv").read_text()
        assert "lib/thirdparty" in manifest
        assert "https://example.invalid/thirdparty.git" in manifest
        # The checkout itself is untouched on disk.
        assert (vendor / ".git").is_dir()

    def test_scope_gets_an_isolated_clone_on_its_own_branch(self, scopes_workspace, project):
        awm = _awm_dir(scopes_workspace, "demo", "s1")
        rep = da.provision_scope_data("demo", "s1", awm)
        assert rep["mode"] == "annex"
        assert rep["branch"] == "scope/s1"
        assert rep["content"] == "fetched"
        assert not (awm / "data").is_symlink()
        assert (awm / "data" / "raw" / "notes.md").read_text() == "# small\n"
        # Exclusions are inherited by the clone.
        assert not (awm / "data" / "secrets").exists()

    def test_content_is_hardlinked_not_copied(self, scopes_workspace, project):
        a = _awm_dir(scopes_workspace, "demo", "s1")
        b = _awm_dir(scopes_workspace, "demo", "s2")
        da.provision_scope_data("demo", "s1", a)
        da.provision_scope_data("demo", "s2", b)
        inodes = {
            os.stat(p / "data" / "raw" / "bulk.bin").st_ino for p in (a, b)
        } | {os.stat(project / "raw" / "bulk.bin").st_ino}
        assert len(inodes) == 1, "content was copied instead of hardlinked"

    def test_clone_is_untrusted(self, scopes_workspace, project):
        """A hardlink is not an independent copy, so the clone must not count."""
        awm = _awm_dir(scopes_workspace, "demo", "s1")
        da.provision_scope_data("demo", "s1", awm)
        out = _sh("git", "-C", str(awm / "data"), "annex", "whereis",
                  "raw/bulk.bin").stdout
        assert "(1 copy)" in out, out
        assert "untrusted" in out, out

    def test_scopes_are_isolated_until_promotion(self, scopes_workspace, project):
        a = _awm_dir(scopes_workspace, "demo", "s1")
        b = _awm_dir(scopes_workspace, "demo", "s2")
        da.provision_scope_data("demo", "s1", a)
        da.provision_scope_data("demo", "s2", b)

        (a / "data" / "out.bin").write_bytes(os.urandom(200_000))
        assert da.snapshot_data(a / "data", "s1: out")["result"] == "committed"
        assert not (b / "data" / "out.bin").exists()

        prom = da.promote_data("demo", "s1", a.parent)
        assert prom["result"] == "promoted", prom
        # The canonical WORKING TREE moves too, so every absolute path into
        # data/<project> keeps seeing current data.
        assert (project / "out.bin").exists()

    def test_conflicting_writes_are_reported_and_rolled_back(
            self, scopes_workspace, project):
        a = _awm_dir(scopes_workspace, "demo", "s1")
        b = _awm_dir(scopes_workspace, "demo", "s2")
        da.provision_scope_data("demo", "s1", a)
        da.provision_scope_data("demo", "s2", b)
        for awm, tag in ((a, b"s1"), (b, b"s2")):
            (awm / "data" / "raw" / "bulk.bin").unlink()
            (awm / "data" / "raw" / "bulk.bin").write_bytes(os.urandom(300_000) + tag)
            da.snapshot_data(awm / "data", f"{tag.decode()}: rewrite")
        assert da.publish_data(b / "data")["result"] == "published"

        clone = a / "data"
        _sh("git", "-C", str(clone), "fetch", "origin")
        _sh("git", "-C", str(clone), "annex", "merge")
        before = da.head_rev(clone)
        res = da.merge_data(clone, "origin/scope/s2", "gather s2", label="s2")

        assert res["result"] == "conflict", res
        assert da.head_rev(clone) == before, "merge was not rolled back"
        assert da._is_clean(clone), "worktree left dirty after a conflict"
        # git-annex only invents `file.variant-<key>` under its OWN merge
        # machinery; a plain merge must surface the conflict instead.
        assert not list((clone / "raw").glob("*.variant-*"))

    def test_promotion_refuses_rather_than_clobbering(self, scopes_workspace, project):
        a = _awm_dir(scopes_workspace, "demo", "s1")
        b = _awm_dir(scopes_workspace, "demo", "s2")
        da.provision_scope_data("demo", "s1", a)
        da.provision_scope_data("demo", "s2", b)
        for awm, tag in ((a, b"s1"), (b, b"s2")):
            (awm / "data" / "race.bin").write_bytes(os.urandom(200_000) + tag)
            da.snapshot_data(awm / "data", "race")

        first = da.promote_data("demo", "s1", a.parent)
        second = da.promote_data("demo", "s2", b.parent)

        assert first["result"] == "promoted", first
        assert second["result"] in ("conflict", "rejected", "blocked"), second
        winner = (a / "data" / "race.bin").read_bytes()
        assert (project / "race.bin").read_bytes() == winner

    def test_teardown_refuses_to_destroy_the_only_copy(self, scopes_workspace, project):
        awm = _awm_dir(scopes_workspace, "demo", "s1")
        da.provision_scope_data("demo", "s1", awm)
        (awm / "data" / "only-here.bin").write_bytes(os.urandom(200_000))
        da.snapshot_data(awm / "data", "unpublished")
        # Sever the route to the canonical repo so publishing cannot succeed.
        _sh("git", "-C", str(awm / "data"), "remote", "set-url", "origin",
            str(scopes_workspace["workspace"] / "gone"))

        guard = da.prepare_teardown(awm.parent, project="demo", scope="s1")
        assert guard["result"] == "refused", guard
        assert guard["orphans"] >= 1

        forced = da.prepare_teardown(awm.parent, project="demo", scope="s1", force=True)
        assert forced["result"] == "ok"
        # chmod ran, so the caller's rmtree can now complete.
        assert os.access(awm / "data" / ".git" / "annex" / "objects", os.W_OK)

    def test_heal_converts_a_legacy_symlink_scope(self, scopes_workspace, project):
        """The migration path for the scopes that pre-date this layer."""
        from awm.scopes.scopes import _heal_data
        awm = _awm_dir(scopes_workspace, "demo", "s1")
        (awm / "data").symlink_to(project)          # the old shared symlink
        action = _heal_data(awm, project="demo", scope="s1", dry_run=False)
        assert action == "converted-from-symlink"
        assert da.is_annex_repo(awm / "data")
        assert da.current_branch(awm / "data") == "scope/s1"

    def test_status_reports_drift(self, scopes_workspace, project):
        awm = _awm_dir(scopes_workspace, "demo", "s1")
        da.provision_scope_data("demo", "s1", awm)
        (awm / "data" / "new.bin").write_bytes(os.urandom(200_000))
        da.snapshot_data(awm / "data", "one commit ahead")
        st = da.data_status("demo", "s1", awm.parent)
        assert st["mode"] == "annex"
        assert st["branch"] == "scope/s1"
        assert st["ahead_of_canonical"] == 1
        assert st["behind_canonical"] == 0
        assert st["dirty"] is False
