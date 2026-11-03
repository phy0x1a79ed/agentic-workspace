"""Keep every test off the live gateway: usage reports land in ``usage_events``, and cch
reports no live session unless a test says so."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def usage_events(monkeypatch):
    import awm.usage

    events: list[tuple] = []
    monkeypatch.setattr(awm.usage, "record_background",
                        lambda job, ok=True, ms=None, *, plugin=None: events.append((plugin, job, ok, ms)))
    monkeypatch.setattr(awm.usage, "post", events.append)
    return events


@pytest.fixture(autouse=True)
def _no_live_sessions(monkeypatch):
    from awm.transcripts import live
    monkeypatch.setattr(live, "_ask", lambda: {"ok": True, "session_ids": []})
