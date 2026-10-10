"""`mode_of`: the gateway-readable tri-state answer for a session's mode.

Everything is read from files, so each test builds a temporary Claude home with
a roster, job records, session records and cx's lineage records. Live pids
(this process and its parent) stand in for REPLs, since the roster pairs a pid
with that process's start time.
"""

from __future__ import annotations

import json
import os

import pytest

from awm.claudedaemon import sessionmode
from awm.claudedaemon.sessionmode import UNKNOWN, mode_of

ME = os.getpid()
JOB = "abababab"


def _proc_start(pid: int) -> str:
    with open(f"/proc/{pid}/stat") as fh:
        return fh.read().partition(") ")[2].split()[19]


class Home:
    def __init__(self, root):
        self.root = root
        self.roster = root / "daemon" / "roster.json"
        self.jobs = root / "jobs"
        self.sessions = root / "sessions"
        self.starts = root / "cx" / "starts"
        for d in (self.roster.parent, self.jobs, self.sessions, self.starts):
            d.mkdir(parents=True)
        self.roster.write_text(json.dumps({"workers": {}}))

    def worker(self, pid=ME, job=JOB, name="rep", proc_start=None):
        self.roster.write_text(json.dumps({"workers": {job: {
            "replPid": pid, "replProcStart": proc_start or _proc_start(pid),
            "dispatch": {"seed": {"name": name}}}}}))
        (self.jobs / job).mkdir(exist_ok=True)
        (self.jobs / job / "state.json").write_text(json.dumps({"name": name}))

    def lineage(self, mode, job=JOB):
        (self.starts / f"{job}.json").write_text(json.dumps({"mode": mode}))

    def pending(self, name, mode):
        sessionmode.pending_path(name).write_text(
            json.dumps({"name": name, "mode": mode, "pending": True}))

    def record(self, pid, kind, job=None, name="x", proc_start=None):
        (self.sessions / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "procStart": proc_start or _proc_start(pid), "kind": kind,
            "jobId": job, "name": name}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("AWM_CX_ROSTER", "AWM_CX_JOBS", "AWM_CX_SESSIONS", "AWM_CX_STATE"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return Home(root)


def test_a_cx_started_session_answers_its_declared_mode(home):
    home.worker()
    home.lineage("representative")
    assert mode_of(ME) == "representative"


def test_a_pending_start_gives_its_mode_to_the_session_of_that_name(home):
    home.worker(name="rep")
    home.pending("rep", "secretary")
    assert mode_of(ME) == "secretary"


def test_a_bg_session_missing_from_the_roster_uses_its_lineage(home):
    home.record(ME, "bg", JOB)
    home.lineage("representative")
    assert mode_of(ME) == "representative"


def test_a_pid_with_no_record_is_not_cx_started(home):
    assert mode_of(ME) is None


def test_an_interactive_session_is_not_cx_started(home):
    home.record(ME, "interactive")
    assert mode_of(ME) is None


def test_a_roster_session_with_no_lineage_is_not_cx_started(home):
    home.worker(name="<warm otter>")
    assert mode_of(ME) is None


@pytest.mark.parametrize("bad", [None, 0, -3, True, "12", 1.5])
def test_an_invalid_pid_is_unknown(home, bad):
    assert mode_of(bad) == UNKNOWN


def test_a_bg_session_with_no_record_of_its_start_is_unknown(home):
    home.record(ME, "bg", JOB)
    assert mode_of(ME) == UNKNOWN


def test_an_unreadable_roster_is_unknown(home):
    home.worker()
    home.lineage("representative")
    home.roster.write_text("{ not json")
    assert mode_of(ME) == UNKNOWN


def test_a_roster_that_is_not_an_object_is_unknown(home):
    home.roster.write_text("[]")
    assert mode_of(ME) == UNKNOWN


@pytest.mark.parametrize("what", ["jobs", "sessions"])
def test_a_missing_jobs_or_sessions_directory_leaves_the_declared_mode(home, what):
    import shutil

    home.worker()
    home.lineage("representative")
    shutil.rmtree({"jobs": home.jobs, "sessions": home.sessions}[what])
    assert mode_of(ME) == "representative"


# --- a caller is held only to its own records ---------------------------------


def test_an_interactive_caller_never_touches_the_roster_or_lineage(home):
    home.record(ME, "interactive")
    home.roster.write_text("{ not json")
    import shutil

    shutil.rmtree(home.starts)
    assert mode_of(ME) is None


def test_a_non_bg_record_answers_none_before_a_damaged_roster(home):
    home.record(ME, "sdk")
    home.roster.write_text("[]")
    (home.starts / "pending-0123456789abcdef.json").write_text("{ not json")
    assert mode_of(ME) is None


def test_a_caller_with_no_roster_and_no_starts_directory_is_not_cx_started(home):
    import shutil

    home.roster.unlink()
    shutil.rmtree(home.starts)
    assert mode_of(ME) is None


def test_a_corrupt_lineage_record_of_another_job_changes_nothing(home):
    home.worker()
    home.lineage("representative")
    (home.starts / "cccccccc.json").write_text("{ not json")
    assert mode_of(ME) == "representative"


def test_a_corrupt_pending_file_of_another_name_changes_nothing(home):
    home.worker(name="rep")
    home.pending("rep", "secretary")
    sessionmode.pending_path("someone-else").write_text("{ not json")
    (home.starts / "pending-0123456789abcdef.json").write_text("[1]")
    assert mode_of(ME) == "secretary"


def test_a_corrupt_pending_file_of_the_callers_own_name_is_unknown(home):
    home.worker(name="rep")
    sessionmode.pending_path("rep").write_text("{ not json")
    assert mode_of(ME) == UNKNOWN


def test_a_corrupt_lineage_record_of_the_caller_is_unknown(home):
    home.worker()
    (home.starts / f"{JOB}.json").write_text("{ not json")
    assert mode_of(ME) == UNKNOWN


def test_a_bg_caller_reads_its_own_lineage_without_the_roster(home):
    home.record(ME, "bg", JOB)
    home.lineage("secretary")
    home.roster.write_text("{ not json")
    assert mode_of(ME) == "secretary"


def test_a_bg_caller_with_an_unreadable_roster_and_no_lineage_is_unknown(home):
    home.record(ME, "bg", JOB)
    home.roster.write_text("{ not json")
    assert mode_of(ME) == UNKNOWN


def test_a_bg_record_without_a_job_id_is_unknown(home):
    home.record(ME, "bg", None)
    assert mode_of(ME) == UNKNOWN


# --- a job that looks cx-started but has no lineage ---------------------------


def _launched_with(home, args, key="args"):
    roster_data = json.loads(home.roster.read_text())
    worker = roster_data["workers"][JOB]
    if key == "args":
        worker["dispatch"]["launch"] = {"args": args}
    else:
        worker[key] = args
    home.roster.write_text(json.dumps(roster_data))


@pytest.mark.parametrize("args,key", [
    (["--permission-mode=dontAsk"], "args"), (["--permission-mode", "plan"], "args"),
    (["--restricted"], "args"), (["--permission-mode=dontAsk"], "respawnFlags"),
])
def test_a_job_launched_with_cx_flags_but_no_lineage_is_unknown(home, args, key):
    home.worker()
    _launched_with(home, args, key)
    assert mode_of(ME) == UNKNOWN


def test_a_bg_record_for_a_cx_launched_job_with_no_lineage_is_unknown(home):
    home.worker()
    home.record(ME, "bg", JOB)
    _launched_with(home, ["--permission-mode=dontAsk"])
    assert mode_of(ME) == UNKNOWN


def test_a_job_with_the_pool_flags_and_no_lineage_is_not_cx_started(home):
    home.worker()
    _launched_with(home, ["--dangerously-skip-permissions", "--effort", "medium"])
    assert mode_of(ME) is None


def test_a_cx_launched_job_with_its_lineage_answers_the_mode(home):
    home.worker()
    _launched_with(home, ["--permission-mode=dontAsk"])
    home.lineage("representative")
    assert mode_of(ME) == "representative"


@pytest.mark.parametrize("bad", ["has space", "", 7, None, "x" * 41])
def test_an_invalid_recorded_mode_is_unknown(home, bad):
    home.worker()
    home.lineage(bad)
    assert mode_of(ME) == UNKNOWN


def test_a_stale_session_record_is_unknown(home):
    home.record(ME, "bg", JOB, proc_start="1")
    assert mode_of(ME) == UNKNOWN


def test_a_recycled_roster_pid_is_not_taken_for_the_session(home):
    home.worker(proc_start="1")
    home.lineage("representative")
    assert mode_of(ME) is None


def test_the_paths_follow_cx_environment_overrides(home, tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_CX_ROSTER", str(tmp_path / "r.json"))
    monkeypatch.setenv("AWM_CX_JOBS", str(tmp_path / "j"))
    monkeypatch.setenv("AWM_CX_SESSIONS", str(tmp_path / "s"))
    monkeypatch.setenv("AWM_CX_STATE", str(tmp_path / "st"))
    assert sessionmode.roster_path() == tmp_path / "r.json"
    assert sessionmode.jobs_dir() == tmp_path / "j"
    assert sessionmode.sessions_dir() == tmp_path / "s"
    assert sessionmode.state_dir() == tmp_path / "st"
    assert sessionmode.starts_dir() == tmp_path / "st" / "starts"


def test_the_default_paths_sit_under_the_claude_home(home):
    assert sessionmode.starts_dir() == home.root / "cx" / "starts"
    assert sessionmode.roster_path() == home.roster
