"""The timer's sweep counts as one background run of the transcripts plugin (R132)."""

from __future__ import annotations

import pytest

from awm.transcripts import nightly


@pytest.mark.parametrize("failed, ok, code", [([], True, 0), (["s1"], False, 1)])
def test_a_nightly_sweep_is_counted_with_its_outcome(monkeypatch, usage_events, failed, ok, code):
    monkeypatch.setattr(nightly.runs, "init", lambda: None)
    monkeypatch.setattr(nightly.runs.RunsDAO, "record", lambda self, *a, **k: 1)
    monkeypatch.setattr(nightly.sweep, "archive", lambda days: {"failed": failed})
    assert nightly.main() == code
    ((plugin, job, got_ok, ms),) = usage_events
    assert (plugin, job, got_ok) == ("transcripts", "nightly", ok) and ms >= 0
