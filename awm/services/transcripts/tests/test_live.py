"""Which sessions the sweep must treat as running, over a fake `~/.claude`."""

from __future__ import annotations

import json
import os

import pytest

from awm.claudedaemon import roster
from awm.transcripts import live


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "jobs" / "j1").mkdir(parents=True)
    monkeypatch.setattr(live, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(live, "ROSTER", tmp_path / "roster.json")
    monkeypatch.setattr(live, "JOBS", tmp_path / "jobs")
    return tmp_path


def test_every_place_that_names_a_running_session_is_read(home):
    (home / "sessions" / f"{os.getpid()}.json").write_text('{"sessionId": "tty"}')
    (home / "sessions" / "999999999.json").write_text('{"sessionId": "gone"}')
    (home / "roster.json").write_text('{"workers": {"j1": {"sessionId": "bg"}}}')
    (home / "jobs" / "j1" / "state.json").write_text(
        json.dumps({"resumeSessionId": "resumed"}))
    assert live.session_ids() == {"tty", "bg", "resumed"}


def test_an_absent_home_has_nothing_live(home):
    assert live.session_ids() == set()


def test_a_torn_roster_stops_the_sweep_rather_than_emptying_the_list(home):
    (home / "roster.json").write_text('{"workers": ')
    with pytest.raises(roster.Unreadable):
        live.session_ids()


def test_a_corrupt_job_record_is_named_in_the_error_and_the_log(home, caplog):
    bad = home / "jobs" / "j1" / "state.json"
    bad.write_text("{torn")
    with caplog.at_level("ERROR", logger="awm.transcripts.live"):
        with pytest.raises(roster.Unreadable) as exc:
            live.session_ids()
    assert exc.value.path == bad
    assert str(bad) in str(exc.value)
    assert str(bad) in caplog.text
