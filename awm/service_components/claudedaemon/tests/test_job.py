"""Typing a line into a background session by job id, against a fake home."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from awm.claudedaemon import job, roster


@pytest.fixture
def home(tmp_path):
    for d in ("daemon", "jobs", "sessions"):
        (tmp_path / d).mkdir()
    return tmp_path


def seed(home, *, status="idle", pid=None, proc_start=None, sock="/run/x.sock"):
    pid = pid or os.getpid()
    start = proc_start if proc_start is not None else roster.proc_start(pid)
    worker = {"replPid": pid, "replProcStart": start, "ptySock": sock, "ptyAuth": "tok",
              "sessionId": "sess-1", "startedAt": 1, "decModes": [2004]}
    (home / "daemon" / "roster.json").write_text(
        json.dumps({"supervisorPid": os.getpid(), "workers": {"abc12345": worker}}))
    (home / "jobs" / "abc12345").mkdir()
    (home / "jobs" / "abc12345" / "state.json").write_text(json.dumps({"name": "front-door"}))
    (home / "sessions" / f"{pid}.json").write_text(
        json.dumps({"kind": "bg", "status": status, "procStart": roster.proc_start(pid)}))


def paths(home):
    return dict(roster_path=home / "daemon" / "roster.json", jobs_dir=home / "jobs",
                sessions_dir=home / "sessions")


def test_lane_for_addresses_a_live_job_from_the_roster(home):
    seed(home)
    lane = job.lane_for("abc12345", **paths(home))
    assert (lane.sock, lane.auth, lane.session_id, lane.name) == ("/run/x.sock", "tok", "sess-1", "front-door")
    assert lane.dec_modes == (2004,)


def test_lane_for_refuses_an_unknown_job(home):
    seed(home)
    with pytest.raises(job.JobUnavailable, match="not in the daemon roster"):
        job.lane_for("nope0000", **paths(home))


def test_lane_for_refuses_a_job_whose_process_is_gone(home):
    seed(home, proc_start="1")
    with pytest.raises(job.JobUnavailable, match="no longer running"):
        job.lane_for("abc12345", **paths(home))


@pytest.mark.parametrize("status", ["idle", "busy"])
def test_lane_for_accepts_only_the_safe_statuses(home, status):
    seed(home, status=status)
    assert job.lane_for("abc12345", **paths(home)).session_id == "sess-1"


@pytest.mark.parametrize("status", ["waiting", "", "needs-input", "shutting-down"])
def test_lane_for_refuses_every_other_status(home, status):
    seed(home, status=status)
    with pytest.raises(job.JobUnavailable, match="not idle or busy"):
        job.lane_for("abc12345", **paths(home))


def test_lane_for_reads_the_paths_cx_is_told_to_use(home, monkeypatch):
    seed(home)
    monkeypatch.setenv("AWM_CX_ROSTER", str(home / "daemon" / "roster.json"))
    monkeypatch.setenv("AWM_CX_JOBS", str(home / "jobs"))
    monkeypatch.setenv("AWM_CX_SESSIONS", str(home / "sessions"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "nowhere"))
    assert job.lane_for("abc12345").session_id == "sess-1"


def test_lane_for_refuses_a_job_with_no_pty(home):
    seed(home, sock="")
    with pytest.raises(job.JobUnavailable, match="no reachable PTY"):
        job.lane_for("abc12345", **paths(home))


def test_send_line_pastes_then_commits(home, monkeypatch):
    seed(home)
    calls = []

    class Conn:
        def write(self, text):
            calls.append(("write", text))

        def commit(self):
            calls.append(("commit",))

    @contextmanager
    def fake_open_lane(lane, **kw):
        calls.append(("open", lane.session_id))
        yield Conn()

    monkeypatch.setattr(job, "open_lane", fake_open_lane)
    job.send_line("abc12345", "3 new cards, run door list", **paths(home))
    assert calls == [("open", "sess-1"), ("write", "3 new cards, run door list"), ("commit",)]


def test_send_line_refuses_multi_line_text_before_opening_anything(home, monkeypatch):
    seed(home)
    monkeypatch.setattr(job, "open_lane", lambda *a, **k: pytest.fail("opened a lane"))
    with pytest.raises(ValueError):
        job.send_line("abc12345", "a\nb", **paths(home))
