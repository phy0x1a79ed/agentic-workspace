"""Tests for awm.artifacts — registration, sync, and DAO surface.

Tests that exercise register_artifact require a live scopes service for the
resolveScope RPC call — those are marked ``integration`` and skipped unless
AWM_HUB_URL is set and the scopes service is reachable. All other tests
(sync, DAO, seed) run without any live services.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from awm.artifacts.dao import ArtifactsDAO, init, SERVICE, SCHEMA_SQL
from awm.artifacts.models import ArtifactRegisterRequest, ArtifactInfo
from awm.artifacts.artifacts import (
    sync_artifacts,
    now_ms,
    ms_to_iso,
    _validate_name,
)
from awm.persistence.databases import init_service_db, get_connection

pytestmark = [pytest.mark.artifacts]


# ---------------------------------------------------------------------------
# Fixtures — isolated service DB in a temp dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_artifacts_db(tmp_path, monkeypatch):
    """Route the artifacts and config service DBs into a temp dir for each test."""
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    monkeypatch.setenv("AWM_WORKSPACE", str(tmp_path))
    # Patch SERVICES_DIR in the databases module so all service DBs go into tmp
    import awm.persistence.databases as dbmod
    monkeypatch.setattr(dbmod, "SERVICES_DIR", services_dir, raising=False)
    # Reset init flag so each test gets a fresh artifacts DB
    import awm.artifacts.dao as daomod
    monkeypatch.setattr(daomod, "_initialized", False)
    # Reset config service init flag so it creates in the temp dir
    import awm.persistence.config_service as cfgmod
    monkeypatch.setattr(cfgmod, "_initialized", False)
    init()
    yield tmp_path


@pytest.fixture
def workspace(isolated_artifacts_db):
    return isolated_artifacts_db


@pytest.fixture
def dao():
    return ArtifactsDAO()


# ---------------------------------------------------------------------------
# Helper: insert an artifact row directly (bypassing RPC for unit tests)
# ---------------------------------------------------------------------------


def _direct_insert(project: str, scope: str, path: str, name: str = "fig1") -> int:
    """Insert an artifact row directly into the DB (no RPC validation)."""
    dao = ArtifactsDAO()
    ts = now_ms()
    return dao.insert_artifact(
        project=project,
        scope=scope,
        name=name,
        artifact_type="figure",
        path=path,
        description="test",
        format=None,
        tags=None,
        created_at=ts,
        updated_at=ts,
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


class TestTimeHelpers:
    def test_now_ms_is_int(self):
        ts = now_ms()
        assert isinstance(ts, int)
        assert ts > 0

    def test_ms_to_iso_roundtrip(self):
        ts = now_ms()
        iso = ms_to_iso(ts)
        assert iso is not None
        assert "T" in iso  # ISO 8601 format

    def test_ms_to_iso_none_for_zero(self):
        assert ms_to_iso(0) is None
        assert ms_to_iso(None) is None


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


class TestValidateName:
    def test_valid_names_pass(self):
        assert _validate_name("myproject") == "myproject"

    def test_slash_rejected(self):
        with pytest.raises(ValueError, match="cannot contain"):
            _validate_name("proj/scope")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_name("")

    def test_dot_prefix_rejected(self):
        with pytest.raises(ValueError, match="cannot start with"):
            _validate_name(".hidden")


# ---------------------------------------------------------------------------
# DAO surface
# ---------------------------------------------------------------------------


class TestArtifactsDAO:
    def test_insert_and_get_by_id(self, dao):
        ts = now_ms()
        artifact_id = dao.insert_artifact(
            project="proj", scope="scope-a",
            name="fig1", artifact_type="figure",
            path="data/proj/fig1.png",
            description="A figure", format="png", tags="chart",
            created_at=ts, updated_at=ts,
        )
        row = dao.get_by_id(artifact_id)
        assert row is not None
        assert row["project"] == "proj"
        assert row["scope"] == "scope-a"
        assert row["name"] == "fig1"
        assert row["artifact_type"] == "figure"
        assert row["status"] == "current"

    def test_search_by_project(self, dao):
        ts = now_ms()
        dao.insert_artifact(
            project="projA", scope="s1", name="a1",
            artifact_type="figure", path="data/a1.png",
            description=None, format=None, tags=None,
            created_at=ts, updated_at=ts,
        )
        dao.insert_artifact(
            project="projB", scope="s2", name="b1",
            artifact_type="figure", path="data/b1.png",
            description=None, format=None, tags=None,
            created_at=ts, updated_at=ts,
        )
        results = dao.search(project="projA")
        assert len(results) == 1
        assert results[0]["project"] == "projA"

    def test_delete_artifact(self, dao):
        ts = now_ms()
        aid = dao.insert_artifact(
            project="proj", scope="s1", name="a1",
            artifact_type="figure", path="data/a.png",
            description=None, format=None, tags=None,
            created_at=ts, updated_at=ts,
        )
        deleted = dao.delete_artifact(aid)
        assert deleted is not None
        assert deleted["id"] == aid
        assert dao.get_by_id(aid) is None

    def test_get_fingerprint_changes_on_insert(self, dao):
        fp1 = dao.get_fingerprint()
        ts = now_ms()
        dao.insert_artifact(
            project="proj", scope="s1", name="x",
            artifact_type="figure", path="data/x.png",
            description=None, format=None, tags=None,
            created_at=ts, updated_at=ts,
        )
        fp2 = dao.get_fingerprint()
        assert fp1 != fp2

    def test_mark_stale_and_restore(self, dao):
        ts = now_ms()
        aid = dao.insert_artifact(
            project="proj", scope="s1", name="a",
            artifact_type="figure", path="data/a.png",
            description=None, format=None, tags=None,
            created_at=ts, updated_at=ts,
        )
        dao.mark_stale([aid], now_ms())
        row = dao.get_by_id(aid)
        assert row["status"] == "stale"

        dao.restore_current([aid], now_ms())
        row = dao.get_by_id(aid)
        assert row["status"] == "current"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class TestSyncArtifacts:
    def test_flips_missing_file_to_stale(self, workspace):
        path = "data/proj/fig1.png"
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"fake")
        aid = _direct_insert("proj", "scope-a", path)

        # Remove the file so sync marks it stale
        full.unlink()

        # Patch WORKSPACE_ROOT in artifacts module
        with patch("awm.artifacts.artifacts.WORKSPACE_ROOT", workspace):
            result = sync_artifacts(force=True)

        assert result["skipped"] is False
        assert result["marked_stale"] == 1

        dao = ArtifactsDAO()
        row = dao.get_by_id(aid)
        assert row["status"] == "stale"

    def test_restores_reappeared_file(self, workspace):
        path = "data/proj/fig1.png"
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"fake")
        _direct_insert("proj", "scope-a", path)

        full.unlink()
        with patch("awm.artifacts.artifacts.WORKSPACE_ROOT", workspace):
            sync_artifacts(force=True)

        full.write_bytes(b"fake")
        with patch("awm.artifacts.artifacts.WORKSPACE_ROOT", workspace):
            result = sync_artifacts(force=True)

        assert result["restored"] == 1
        row = ArtifactsDAO().get_by_id(1)
        assert row["status"] == "current"

    def test_second_call_is_lazy_noop(self, workspace):
        path = "data/proj/fig1.png"
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"fake")
        _direct_insert("proj", "scope-a", path)

        with patch("awm.artifacts.artifacts.WORKSPACE_ROOT", workspace):
            first = sync_artifacts()
            assert first["skipped"] is False
            second = sync_artifacts()
            assert second["skipped"] is True

    def test_force_bypasses_fingerprint(self, workspace):
        path = "data/proj/fig1.png"
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"fake")
        _direct_insert("proj", "scope-a", path)

        with patch("awm.artifacts.artifacts.WORKSPACE_ROOT", workspace):
            sync_artifacts()
            result = sync_artifacts(force=True)
        assert result["skipped"] is False


# ---------------------------------------------------------------------------
# Register — requires live scopes RPC (integration tests)
# ---------------------------------------------------------------------------

_SKIP_INTEGRATION = pytest.mark.skipif(
    not os.environ.get("AWM_HUB_URL"),
    reason="AWM_HUB_URL not set; scopes service not available",
)


@_SKIP_INTEGRATION
class TestRegisterArtifactIntegration:
    """These tests require a live scopes service reachable via AWM_HUB_URL."""

    def test_register_fails_on_unknown_scope(self):
        from awm.artifacts.artifacts import register_artifact
        with pytest.raises(ValueError, match="does not exist"):
            register_artifact(ArtifactRegisterRequest(
                project="__no_such_project__",
                scope="__no_such_scope__",
                name="x",
                artifact_type="figure",
                path="data/x.png",
            ))

    def test_register_and_search(self, workspace):
        # Assumes (proj, scope-a) exists in the live scopes service
        from awm.artifacts.artifacts import register_artifact, search_artifacts
        path = "data/proj/fig1.png"
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(b"content")

        info = register_artifact(ArtifactRegisterRequest(
            project="proj", scope="scope-a",
            name="fig1", artifact_type="figure", path=path,
        ))
        assert info.project == "proj"
        assert info.scope == "scope-a"

        results = search_artifacts(project="proj")
        ids = [a.id for a in results.artifacts]
        assert info.id in ids
