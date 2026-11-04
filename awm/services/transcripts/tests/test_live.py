"""The sweep's live list comes from cch, and no answer stops the sweep."""

from __future__ import annotations

import pytest

from awm.transcripts import live


def test_the_live_sessions_are_cchs_answer(monkeypatch):
    monkeypatch.setattr(live, "_ask", lambda: {"ok": True, "session_ids": ["a", "b"]})
    assert live.session_ids() == {"a", "b"}


@pytest.mark.parametrize("answer", [
    {"ok": False, "reason": "unreadable /x/state.json"}, None, "nonsense"])
def test_a_refusal_stops_the_sweep(monkeypatch, answer, caplog):
    monkeypatch.setattr(live, "_ask", lambda: answer)
    with caplog.at_level("ERROR", logger="awm.transcripts.live"):
        with pytest.raises(live.Unknown):
            live.session_ids()
    assert "sweep blocked" in caplog.text


def test_no_answer_stops_the_sweep(monkeypatch):
    def down():
        raise ConnectionError("gateway down")

    monkeypatch.setattr(live, "_ask", down)
    with pytest.raises(live.Unknown):
        live.session_ids()
