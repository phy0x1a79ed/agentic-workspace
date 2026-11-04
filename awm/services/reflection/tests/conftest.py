"""Keep every test off the live node: no gateway calls, no writes to the real state dir."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stays_local(monkeypatch, tmp_path):
    from awm.reflection import alarm, harness
    monkeypatch.setattr(harness, "_down", {})
    monkeypatch.setattr(alarm, "STATE_DIR", tmp_path / "state")
    posts: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(alarm, "_call", lambda s, f, a: posts.append((s, f, a)))
    return posts
