"""Keep every test off the live node: no journal posts, no writes to the real state dir."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _alarm_stays_local(monkeypatch, tmp_path):
    from awm.reflection import alarm, inject, pending
    # A dead pid waits this long for a successor process; tests that care set it.
    monkeypatch.setattr(inject, "SUCCESSOR_WAIT_S", 0.0)
    posts: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(alarm, "_call", lambda s, f, a: posts.append((s, f, a)))
    monkeypatch.setattr(pending, "PENDING_DIR", tmp_path / "pending-default")
    return posts
