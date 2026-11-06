"""Keep the sync lock out of the node's real state."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _lock_in_tmp(tmp_path, monkeypatch):
    from awm.zotero import sync
    monkeypatch.setattr(sync, "STATE_DIR", tmp_path / "zotero-state")
    monkeypatch.setattr(sync, "LOCK_FILE", tmp_path / "zotero-state" / "sync.lock")
