"""Test helper: the events cch sent reflection during one test."""
from __future__ import annotations


def lost_events() -> list[dict]:
    """The ``lost`` events sent so far in this test, newest first."""
    from awm.cch import events
    return [a for _s, _f, a in reversed(events._call.posts) if a["kind"] == "lost"]
