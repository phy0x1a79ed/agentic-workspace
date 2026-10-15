"""The shared reads over the daemon's roster, job records and session records.

Every test builds a small fake home and passes its paths in, so none of them
reads the real `~/.claude`.
"""
from __future__ import annotations

import json
import os

import pytest

from awm.claudedaemon import roster


def own_start() -> str:
    return roster.proc_start(os.getpid())


@pytest.fixture
def home(tmp_path):
    (tmp_path / "daemon").mkdir()
    (tmp_path / "jobs").mkdir()
    (tmp_path / "sessions").mkdir()
    return tmp_path


def write_roster(home, workers, supervisor=None):
    doc = {"supervisorPid": os.getpid() if supervisor is None else supervisor,
           "workers": workers}
    (home / "daemon" / "roster.json").write_text(json.dumps(doc))
    return home / "daemon" / "roster.json"


def write_job(home, short, **state):
    d = home / "jobs" / short
    d.mkdir()
    (d / "state.json").write_text(json.dumps(state))


def test_proc_start_reads_the_running_process_and_nothing_for_a_missing_one():
    assert roster.proc_start(os.getpid())
    assert roster.proc_start(None) is None
    assert roster.proc_start(0) is None
    assert roster.proc_start(2**22 + 12345) is None


def test_load_joins_the_roster_to_each_jobs_own_record(home):
    path = write_roster(home, {
        "newer": {"replPid": 7, "startedAt": 2000, "cliVersion": "2.1.1",
                  "sessionId": "s2", "decModes": [1, "x", 2],
                  "dispatch": {"source": "cli", "seed": {"name": "<warm a>"}}},
        "older": {"replPid": 8, "startedAt": 1000},
    })
    write_job(home, "newer", name="renamed", tokens=5, intent="x",
              needs=None, cwd="/w")
    got = roster.load(path, home / "jobs")
    assert [s.short for s in got] == ["older", "newer"]
    newer = got[1]
    assert (newer.name, newer.tokens, newer.seed_name) == ("renamed", 5, "<warm a>")
    assert newer.dec_modes == (1, 2)
    assert newer.has_record
    assert not got[0].has_record and got[0].name == ""


def test_load_of_a_missing_or_malformed_roster_is_empty(home):
    assert roster.load(home / "nothing.json", home / "jobs") == []
    (home / "daemon" / "roster.json").write_text("{not json")
    assert roster.load(home / "daemon" / "roster.json", home / "jobs") == []


def test_the_daemon_is_the_supervisor_only_while_its_process_runs(home):
    path = write_roster(home, {})
    assert roster.daemon_pid(path) == os.getpid()
    write_roster(home, {}, supervisor=2**22 + 12345)
    assert roster.daemon_pid(path) is None
    assert roster.daemon_pid(home / "nothing.json") is None


def test_binary_version_is_the_directory_the_symlink_points_into(tmp_path):
    (tmp_path / "2.1.268").mkdir()
    (tmp_path / "claude").symlink_to(tmp_path / "2.1.268")
    assert roster.binary_version(tmp_path / "claude") == "2.1.268"
    assert roster.binary_version(tmp_path / "missing") is None


def test_the_attaching_argv_names_the_session(monkeypatch):
    assert "nobody-has-this-short" not in roster.attached_shorts()


# --- session records by pid -------------------------------------------------


def record(home, pid, **fields):
    (home / "sessions" / f"{pid}.json").write_text(json.dumps(fields))


def test_a_session_record_for_a_running_pid_is_returned(home):
    record(home, os.getpid(), kind="bg", jobId="abc", sessionId="s1",
           procStart=own_start())
    rec = roster.read_session_record(os.getpid(), sessions_dir=home / "sessions")
    assert rec["jobId"] == "abc"
    assert roster.job_of_pid(os.getpid(), sessions_dir=home / "sessions") == "abc"


def test_an_interactive_session_has_no_job(home):
    record(home, os.getpid(), kind="interactive", sessionId="s1")
    assert roster.session_by_pid(os.getpid(), sessions_dir=home / "sessions")
    assert roster.job_of_pid(os.getpid(), sessions_dir=home / "sessions") is None


@pytest.mark.parametrize("why,setup", [
    ("no Claude Code session record", lambda h: None),
    ("could not read", lambda h: (h / "sessions" / f"{os.getpid()}.json").write_text("{")),
    ("stale", lambda h: record(h, os.getpid(), kind="bg", procStart="1")),
])
def test_a_record_that_cannot_be_trusted_is_refused(home, why, setup):
    setup(home)
    with pytest.raises(roster.SessionRecordError, match=why):
        roster.read_session_record(os.getpid(), sessions_dir=home / "sessions")
    assert roster.session_by_pid(os.getpid(), sessions_dir=home / "sessions") is None


def test_a_dead_process_is_refused_whatever_its_record_says(home):
    record(home, 4242, kind="bg")
    with pytest.raises(roster.SessionRecordError, match="is gone"):
        roster.read_session_record(4242, sessions_dir=home / "sessions",
                                   proc_start_fn=lambda pid: None)


@pytest.mark.parametrize("bad", [0, -1, None, True, "12"])
def test_session_by_pid_declines_a_pid_that_is_not_one(home, bad):
    assert roster.session_by_pid(bad, sessions_dir=home / "sessions") is None


# --- ancestry ---------------------------------------------------------------


def test_the_process_tree_reaches_this_process_from_its_parent():
    kids = roster.ppid_children()
    assert os.getpid() in kids.get(os.getppid(), [])
    assert roster.subtree_contains(os.getppid(), os.getpid(), kids)


def test_a_subtree_does_not_contain_its_siblings_or_ancestors():
    kids = {1: [2, 3], 2: [4], 4: []}
    assert roster.subtree_contains(2, 4, kids)
    assert roster.subtree_contains(2, 2, kids)
    assert not roster.subtree_contains(2, 3, kids)
    assert not roster.subtree_contains(4, 1, kids)


def test_a_cycle_in_the_process_table_does_not_loop():
    assert not roster.subtree_contains(1, 9, {1: [2], 2: [1]})


# --- live conversation ids --------------------------------------------------


def live_ids(home):
    return roster.live_session_ids(
        sessions_dir=home / "sessions", roster_path=home / "daemon" / "roster.json",
        jobs_dir=home / "jobs")


def test_live_ids_come_from_all_three_places(home):
    record(home, os.getpid(), sessionId="from-record")
    record(home, 2**22 + 99, sessionId="dead-pid")
    write_roster(home, {"j1": {"sessionId": "from-roster"}})
    write_job(home, "j1", sessionId="job-sid", resumeSessionId="job-resume")
    assert live_ids(home) == {"from-record", "from-roster", "job-sid", "job-resume"}


def test_a_home_with_nothing_in_it_has_no_live_ids(tmp_path):
    assert roster.live_session_ids(
        sessions_dir=tmp_path / "s", roster_path=tmp_path / "r.json",
        jobs_dir=tmp_path / "j") == set()


@pytest.mark.parametrize("break_it", [
    lambda h: (h / "daemon" / "roster.json").write_text("{torn"),
    lambda h: (h / "jobs" / "j1" / "state.json").write_text("[1]"),
    lambda h: (h / "sessions" / f"{os.getpid()}.json").write_text("nope"),
])
def test_an_unreadable_record_is_not_an_empty_one(home, break_it):
    """The sweep deletes what is not live, so "could not tell" must raise."""
    record(home, os.getpid(), sessionId="s")
    write_roster(home, {})
    write_job(home, "j1", sessionId="x")
    break_it(home)
    with pytest.raises(roster.Unreadable):
        live_ids(home)
