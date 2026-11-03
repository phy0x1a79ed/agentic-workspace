"""Keep every test off the live gateway: cch reports no live session unless a test says so."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_live_sessions(monkeypatch):
    from awm.transcripts import live
    monkeypatch.setattr(live, "_ask", lambda: {"ok": True, "session_ids": []})
