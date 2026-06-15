"""Test bootstrap for the telemetry component.

Points the per-service DB factory at a tmp dir so each test gets a fresh
``telemetry_events`` table, and clears the per-DB DDL guard so the
IF-NOT-EXISTS schema actually re-runs against the new path.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tel_env(tmp_path, monkeypatch):
    import awm.persistence.databases as dbs_mod
    import awm.telemetry.store as store_mod

    services = tmp_path / "services"
    services.mkdir()
    monkeypatch.setattr(dbs_mod, "SERVICES_DIR", services)
    store_mod._ensured.clear()
    yield services
    store_mod._ensured.clear()
