"""Tests for awm.services.artifacts — registration and the lazy sync function."""

from __future__ import annotations

from awm.models import ArtifactRegisterRequest
from awm.services import artifacts


def _register(workspace, name="fig1", path="data/proj/figures/fig1.png") -> int:
    # Create the on-disk file so sync sees it as 'current'.
    full = workspace / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"fake")

    info = artifacts.register_artifact(ArtifactRegisterRequest(
        project="proj",
        scope="scope-a",
        name=name,
        artifact_type="figure",
        path=path,
        description="test",
    ))
    return info.id


class TestSyncArtifacts:
    def test_flips_missing_file_to_stale(self, awm_workspace, db_conn):
        workspace = awm_workspace["workspace"]
        aid = _register(workspace)

        # Delete the file out-of-band.
        (workspace / "data/proj/figures/fig1.png").unlink()

        result = artifacts.sync_artifacts(force=True)
        assert result["skipped"] is False
        assert result["marked_stale"] == 1

        row = db_conn.execute(
            "SELECT status FROM artifacts WHERE legacy_id=?", (aid,)
        ).fetchone()
        assert row["status"] == "stale"

        # Embedding should have been pruned alongside the status flip.
        emb = db_conn.execute(
            "SELECT 1 FROM embeddings WHERE source_type='artifact' AND source_id=?",
            (str(aid),),
        ).fetchone()
        assert emb is None

    def test_restores_reappeared_file(self, awm_workspace, db_conn):
        workspace = awm_workspace["workspace"]
        aid = _register(workspace)
        full = workspace / "data/proj/figures/fig1.png"
        full.unlink()
        artifacts.sync_artifacts(force=True)

        # File comes back.
        full.write_bytes(b"fake")
        result = artifacts.sync_artifacts(force=True)
        assert result["restored"] == 1
        row = db_conn.execute(
            "SELECT status FROM artifacts WHERE legacy_id=?", (aid,)
        ).fetchone()
        assert row["status"] == "current"

    def test_second_call_is_lazy_noop(self, awm_workspace):
        _register(awm_workspace["workspace"])
        first = artifacts.sync_artifacts()
        assert first["skipped"] is False
        second = artifacts.sync_artifacts()
        assert second["skipped"] is True

    def test_force_bypasses_fingerprint(self, awm_workspace):
        _register(awm_workspace["workspace"])
        artifacts.sync_artifacts()
        result = artifacts.sync_artifacts(force=True)
        assert result["skipped"] is False
