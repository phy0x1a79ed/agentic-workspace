"""Redirect this dist's SQLite DB into a tmp workspace for every test.

Slices are the first thing this service persists in its own DB (`slices.py`).
Without this, a test that mints, lists, revokes or resolves one would write
into the node's real `trilium.db`.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tmp_service_db(tmp_path, monkeypatch):
    services_dir = tmp_path / ".awm" / "services"
    services_dir.mkdir(parents=True)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)
    # Production creates the table from `hub_adapter._on_start`, which these
    # tests never run -- they call `HANDLERS[...]` directly. Without this, the
    # first slice verb a test calls fails on a table that was never made.
    from awm.trilium import slices
    slices.init()
