"""Keep usage reports out of the live gateway: each test sees them in ``usage_events``."""

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
