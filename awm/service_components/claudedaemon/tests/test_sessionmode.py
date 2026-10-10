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

REAL_MAY_BE_A_SESSION = sessionmode._may_be_a_session
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


@pytest.fixture(autouse=True)
def hermetic_tree(monkeypatch):
    """Walk from this process to its real parent and then to init, and treat no
    ancestor as a possible Claude Code process unless a test says so. The tests
    run under a real session; its records must not leak in."""
    monkeypatch.setattr(sessionmode, "_ppid",
                        lambda pid: {ME: os.getppid()}.get(pid, 1))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: False)


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


def test_an_unreadable_roster_is_unknown(home, monkeypatch):
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
    home.worker()
    home.lineage("representative")
    home.roster.write_text("{ not json")
    assert mode_of(ME) == UNKNOWN


def test_a_roster_that_is_not_an_object_is_unknown(home, monkeypatch):
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
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


# --- a process a restricted session starts ------------------------------------

PARENT = os.getppid()
PARENT_JOB = "cdcdcdcd"


def _restricted_parent(home, mode="delegate"):
    """The test process's real parent as a cx-started background session."""
    home.record(PARENT, "bg", PARENT_JOB)
    (home.starts / f"{PARENT_JOB}.json").write_text(json.dumps({"mode": mode}))


def test_a_child_with_its_own_interactive_record_inherits_its_parents_mode(home):
    _restricted_parent(home)
    home.record(ME, "interactive")
    assert mode_of(ME) == "delegate"


def test_a_child_with_no_record_at_all_inherits_too(home):
    _restricted_parent(home, "representative")
    assert mode_of(ME) == "representative"


def test_the_walk_passes_ancestors_that_are_not_sessions(home, monkeypatch):
    _restricted_parent(home)
    home.record(ME, "interactive")
    monkeypatch.setattr(sessionmode, "_ppid", {ME: 987650, 987650: 987651, 987651: PARENT}.get)
    assert mode_of(ME) == "delegate"


def test_a_plain_worker_ancestor_gives_the_worker_mode(home):
    _restricted_parent(home, "worker")
    home.record(ME, "interactive")
    assert mode_of(ME) == "worker"


def test_an_ancestor_job_with_a_damaged_lineage_makes_the_child_unknown(home):
    _restricted_parent(home)
    (home.starts / f"{PARENT_JOB}.json").write_text("{ not json")
    home.record(ME, "interactive")
    assert mode_of(ME) == UNKNOWN


def test_no_session_ancestor_leaves_an_interactive_process_ungated(home):
    home.record(ME, "interactive")
    assert mode_of(ME) is None


def test_an_ancestor_that_is_a_plain_interactive_session_is_passed_over(home):
    home.record(PARENT, "interactive")
    home.record(ME, "interactive")
    assert mode_of(ME) is None


def test_an_ancestor_with_an_unreadable_interactive_record_is_skipped(home):
    (home.sessions / f"{PARENT}.json").write_text("{ not json")
    home.record(ME, "interactive")
    assert mode_of(ME) is None


def test_the_walk_is_bounded_and_survives_a_cycle(home, monkeypatch):
    home.record(ME, "interactive")
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: ME)
    assert mode_of(ME) is None
    calls = []
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: calls.append(pid) or pid + 1)
    mode_of(ME)
    assert len(calls) <= sessionmode.MAX_ANCESTRY_HOPS


# --- an interactive session that took over a parked job -----------------------


def test_an_interactive_record_with_a_parked_job_keeps_the_jobs_mode(home):
    home.worker()
    home.lineage("representative")
    (home.sessions / f"{ME}.json").write_text(json.dumps({
        "pid": ME, "procStart": _proc_start(ME), "kind": "interactive",
        "parkedJobId": JOB, "name": "rep"}))
    assert mode_of(ME) == "representative"


def test_a_parked_job_without_lineage_is_not_cx_started(home):
    (home.sessions / f"{ME}.json").write_text(json.dumps({
        "pid": ME, "procStart": _proc_start(ME), "kind": "interactive",
        "parkedJobId": "eeeeeeee"}))
    assert mode_of(ME) is None


def test_a_parked_job_with_a_damaged_lineage_is_unknown(home):
    (home.starts / "eeeeeeee.json").write_text("{ not json")
    (home.sessions / f"{ME}.json").write_text(json.dumps({
        "pid": ME, "procStart": _proc_start(ME), "kind": "interactive",
        "parkedJobId": "eeeeeeee"}))
    assert mode_of(ME) == UNKNOWN


def test_a_parked_job_id_that_is_not_a_job_id_is_ignored(home):
    (home.sessions / f"{ME}.json").write_text(json.dumps({
        "pid": ME, "procStart": _proc_start(ME), "kind": "interactive",
        "parkedJobId": "../../x"}))
    assert mode_of(ME) is None


# --- a pid that does not exist ------------------------------------------------


def _dead_pid() -> int:
    pid = 4_999_999
    assert not os.path.exists(f"/proc/{pid}")
    return pid


def test_a_pid_with_no_process_is_unknown_not_ungated(home):
    assert mode_of(_dead_pid()) == UNKNOWN


def test_a_dead_pid_is_unknown_even_with_a_healthy_home_and_a_restricted_ancestor(home):
    _restricted_parent(home)
    assert mode_of(_dead_pid()) == UNKNOWN


def test_a_dead_pid_with_a_leftover_record_is_unknown(home):
    pid = _dead_pid()
    (home.sessions / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "procStart": "1", "kind": "interactive"}))
    assert mode_of(pid) == UNKNOWN


def test_a_running_process_with_no_parents_left_keeps_the_ungated_meaning(home, monkeypatch):
    home.record(ME, "interactive")
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: 1)
    assert mode_of(ME) is None
    # the top of the tree is reached some hops up
    monkeypatch.setattr(sessionmode, "_ppid", {ME: 987650, 987650: 0}.get)
    assert mode_of(ME) is None
    # a parent that cannot be read at a later hop is a death under us, not the top
    monkeypatch.setattr(sessionmode, "_ppid", {ME: 987650, 987650: None}.get)
    assert mode_of(ME) == UNKNOWN


def test_a_process_whose_parent_cannot_be_read_at_once_is_unknown(home, monkeypatch):
    """The process was alive a moment ago, so a missing parent is a death under us."""
    home.record(ME, "interactive")
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: None)
    assert mode_of(ME) == UNKNOWN


def test_a_running_interactive_process_stays_ungated(home):
    home.record(ME, "interactive")
    assert mode_of(ME) is None
    assert mode_of(os.getppid()) is None


# --- an ancestor REPL with no session record ----------------------------------


def _roster_lists(home, pid, job, name="rep"):
    """The roster lists `pid` as the REPL of cx-started job `job`."""
    home.roster.write_text(json.dumps({"workers": {job: {
        "replPid": pid, "replProcStart": _proc_start(pid),
        "dispatch": {"seed": {"name": name}}}}}))


def _wrapper_chain(monkeypatch, *hops):
    chain = dict(zip((ME, *hops), (*hops, 1)))
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: chain.get(pid, 1))


def test_a_proxy_behind_a_wrapper_inherits_the_mode_of_a_record_less_repl(home, monkeypatch):
    """The REPL has no sessions/<pid>.json; its MCP proxy runs under `mamba run`."""
    _roster_lists(home, PARENT, PARENT_JOB)
    (home.starts / f"{PARENT_JOB}.json").write_text(json.dumps({"mode": "delegate"}))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: pid == PARENT)
    _wrapper_chain(monkeypatch, 987650, PARENT)
    assert mode_of(PARENT) == "delegate"
    assert mode_of(ME) == "delegate"  # the child, itself record-less and unlisted
    home.record(ME, "interactive")
    assert mode_of(ME) == "delegate"


def test_a_wrapper_pid_is_judged_by_the_repl_above_it(home, monkeypatch):
    _roster_lists(home, PARENT, PARENT_JOB)
    (home.starts / f"{PARENT_JOB}.json").write_text(json.dumps({"mode": "representative"}))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: pid == PARENT)
    # this process stands in for the wrapper: it has no record and is not listed
    _wrapper_chain(monkeypatch, 987650, PARENT)
    assert mode_of(ME) == "representative"


def test_a_cx_launched_record_less_ancestor_without_lineage_is_unknown(home, monkeypatch):
    _roster_lists(home, PARENT, PARENT_JOB)
    roster_data = json.loads(home.roster.read_text())
    roster_data["workers"][PARENT_JOB]["dispatch"]["launch"] = {
        "args": ["--permission-mode=dontAsk"]}
    home.roster.write_text(json.dumps(roster_data))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
    assert mode_of(ME) == UNKNOWN


def test_a_listed_ancestor_that_cx_did_not_start_is_passed_over(home, monkeypatch):
    _roster_lists(home, PARENT, PARENT_JOB)
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
    assert mode_of(ME) is None


def test_a_damaged_roster_makes_a_session_looking_ancestor_unknown(home, monkeypatch):
    home.roster.write_text("{ not json")
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: pid == PARENT)
    assert mode_of(ME) == UNKNOWN


def test_a_damaged_roster_does_not_lock_out_a_terminal_whose_ancestors_are_shells(home):
    home.roster.write_text("{ not json")
    home.record(ME, "interactive")
    assert mode_of(ME) is None
    assert mode_of(ME) is None


def test_the_roster_is_read_once_however_many_ancestors_are_checked(home, monkeypatch):
    reads = []
    real = sessionmode._workers
    monkeypatch.setattr(sessionmode, "_workers", lambda strict: reads.append(strict) or real(strict=strict))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
    chain = {ME: 987650, 987650: 987651, 987651: 987652, 987652: PARENT, PARENT: 1}
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: chain.get(pid, 1))
    home.record(ME, "interactive")
    assert mode_of(ME) is None
    assert len(reads) == 1


# --- the roster is always consulted; the filter only judges a damaged one ------


def _chain_to_parent(monkeypatch):
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: {ME: 987650, 987650: PARENT}.get(pid, 1))


def test_a_healthy_roster_names_a_repl_the_filter_does_not_recognise(home, monkeypatch):
    """D1: an npm-installed REPL (`node .../cli.js`) must not slip past a healthy roster."""
    _roster_lists(home, PARENT, PARENT_JOB)
    (home.starts / f"{PARENT_JOB}.json").write_text(json.dumps({"mode": "delegate"}))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: False)
    _chain_to_parent(monkeypatch)
    home.record(ME, "interactive")
    assert mode_of(ME) == "delegate"


def test_a_listed_repl_with_a_damaged_lineage_stays_unknown(home, monkeypatch):
    _roster_lists(home, PARENT, PARENT_JOB)
    (home.starts / f"{PARENT_JOB}.json").write_text("{ not json")
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: False)
    _chain_to_parent(monkeypatch)
    assert mode_of(ME) == UNKNOWN


def test_a_damaged_roster_is_read_once_and_fails_only_for_a_possible_session(home, monkeypatch):
    home.roster.write_text("{ not json")
    reads = []
    real = sessionmode._workers
    monkeypatch.setattr(sessionmode, "_workers",
                        lambda strict: reads.append(1) or real(strict=strict))
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: False)
    chain = {ME: 987650, 987650: 987651, 987651: PARENT}
    monkeypatch.setattr(sessionmode, "_ppid", lambda pid: chain.get(pid, 1))
    home.record(ME, "interactive")
    assert mode_of(ME) is None
    assert len(reads) == 1


def test_a_stale_ancestor_record_goes_through_the_roster_like_no_record(home, monkeypatch):
    """D3: a record whose procStart does not match is left by an earlier process."""
    _roster_lists(home, PARENT, PARENT_JOB)
    (home.starts / f"{PARENT_JOB}.json").write_text(json.dumps({"mode": "representative"}))
    home.record(PARENT, "interactive", proc_start="1")  # stale
    home.record(ME, "interactive")
    assert mode_of(ME) == "representative"


def test_a_corrupt_ancestor_record_goes_through_the_roster_too(home):
    _roster_lists(home, PARENT, PARENT_JOB)
    (home.starts / f"{PARENT_JOB}.json").write_text(json.dumps({"mode": "secretary"}))
    (home.sessions / f"{PARENT}.json").write_text("{ not json")
    home.record(ME, "interactive")
    assert mode_of(ME) == "secretary"


def test_a_stale_ancestor_record_that_is_not_listed_is_passed_over(home):
    home.record(PARENT, "bg", PARENT_JOB, proc_start="1")
    home.record(ME, "interactive")
    assert mode_of(ME) is None


def test_the_caller_with_a_damaged_roster_is_unknown_only_if_it_may_be_a_session(
        home, monkeypatch):
    """D5: a record with no procStart is dropped, then the roster is consulted."""
    _bare_record(home, ME, kind="interactive")
    home.roster.write_text("{ not json")
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: False)
    assert mode_of(ME) is None
    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
    assert mode_of(ME) == UNKNOWN


# --- the heuristic ---------------------------------------------------------------

POSITIVE = [
    (["claude", "bg-spare"], None),
    (["/home/tony/.local/bin/claude", "--bg"], "/usr/bin/other"),
    (["/home/tony/.local/share/claude/versions/2.1.296"], None),
    (["/home/tony/.local/share/claude/versions/2.1.296"], "/usr/bin/x"),
    (["something"], "/home/tony/.local/share/claude/versions/2.1.295"),
    (["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"], "/usr/bin/node"),
    (["bun", "/x/claude-code/cli.js"], None),
    (["/usr/bin/node", "/opt/claude/cli.js", "--bg"], "/usr/bin/node"),
]
NEGATIVE = [
    (["bash"], "/usr/bin/bash"),
    (["sshd: tony@pts/2"], None),
    (["tmux: server"], "/usr/bin/tmux"),
    (["-bash"], "/usr/bin/bash"),
    (["python", "/home/tony/x/awm/claudedaemon/run.py", "--dir", "/home/tony/.claude"],
     "/usr/bin/python3"),
    (["python", "-m", "pytest", "/home/tony/claudedaemon/tests"], "/envs/awm/bin/python"),
    (["node", "/srv/app/server.js", "/home/tony/.claude/x"], "/usr/bin/node"),
    (["less", "/home/tony/.claude/CLAUDE.md"], "/usr/bin/less"),
]


@pytest.mark.parametrize("argv,exe", POSITIVE)
def test_the_heuristic_recognises_claude_code(argv, exe):
    assert sessionmode._looks_like_claude(argv, exe)


@pytest.mark.parametrize("argv,exe", NEGATIVE)
def test_the_heuristic_ignores_other_processes_and_claude_looking_arguments(argv, exe):
    assert not sessionmode._looks_like_claude(argv, exe)


def _fake_proc(monkeypatch, argv, exe, *, argv_error=False, exe_error=False):
    import builtins

    real_open, real_readlink = builtins.open, os.readlink

    def fake_open(path, *a, **kw):
        if str(path).endswith("/cmdline") and str(path).startswith("/proc/424242"):
            if argv_error:
                raise PermissionError(13, "denied")
            import io
            return io.BytesIO(b"\0".join(x.encode() for x in argv) + b"\0")
        return real_open(path, *a, **kw)

    def fake_readlink(path, *a, **kw):
        if str(path).startswith("/proc/424242"):
            if exe_error:
                raise PermissionError(13, "denied")
            return exe
        return real_readlink(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os, "readlink", fake_readlink)


def test_an_unreadable_exe_does_not_hide_a_readable_command_line(monkeypatch):
    """D2: `/proc/<pid>/exe` is refused for sshd, sudo and login."""
    _fake_proc(monkeypatch, ["sshd: tony@pts/2"], None, exe_error=True)
    assert REAL_MAY_BE_A_SESSION(424242) is False
    _fake_proc(monkeypatch, ["claude", "--bg"], None, exe_error=True)
    assert REAL_MAY_BE_A_SESSION(424242) is True


def test_an_unreadable_command_line_does_not_hide_a_readable_exe(monkeypatch):
    _fake_proc(monkeypatch, [], "/usr/bin/bash", argv_error=True)
    assert REAL_MAY_BE_A_SESSION(424242) is False
    _fake_proc(monkeypatch, [], "/home/tony/.local/share/claude/versions/2.1.296",
               argv_error=True)
    assert REAL_MAY_BE_A_SESSION(424242) is True


def test_a_process_with_neither_readable_is_assumed_to_be_a_session(monkeypatch):
    _fake_proc(monkeypatch, [], None, argv_error=True, exe_error=True)
    assert REAL_MAY_BE_A_SESSION(424242) is True
    assert REAL_MAY_BE_A_SESSION(10**9) is True  # no such process


def test_the_real_test_interpreter_is_not_a_session():
    assert REAL_MAY_BE_A_SESSION(os.getpid()) is False


# --- what a cx start looks like in the roster -----------------------------------


def _launch(home, **launch):
    data = json.loads(home.roster.read_text())
    data["workers"][JOB]["dispatch"]["launch"] = launch
    home.roster.write_text(json.dumps(data))


def test_a_skip_permissions_cx_start_is_recognised_by_its_equals_form_flags(home):
    home.worker()
    _launch(home, args=["--dangerously-skip-permissions", "--effort=medium", "--model=sonnet[1m]"])
    assert mode_of(ME) == UNKNOWN


def test_the_pool_seed_flags_are_not_a_cx_start(home):
    home.worker()
    _launch(home, args=["--session-id", "x", "-n", "<warm badger>",
                        "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                        "--effort", "medium"])
    assert mode_of(ME) is None


def test_one_equals_form_flag_alone_is_not_enough(home):
    home.worker()
    _launch(home, args=["--model=opus"])
    assert mode_of(ME) is None


def test_a_resumed_cx_start_keeps_its_flags_under_flag_args(home):
    home.worker()
    _launch(home, mode="resume", flagArgs=["--permission-mode=dontAsk"])
    assert mode_of(ME) == UNKNOWN
    _launch(home, mode="resume", flagArgs=["--effort=low", "--model=sonnet"])
    assert mode_of(ME) == UNKNOWN
    _launch(home, mode="resume", flagArgs=["--dangerously-skip-permissions"])
    assert mode_of(ME) is None


def test_respawn_flags_under_dispatch_count(home):
    home.worker()
    data = json.loads(home.roster.read_text())
    data["workers"][JOB]["dispatch"]["respawnFlags"] = ["--restricted"]
    home.roster.write_text(json.dumps(data))
    assert mode_of(ME) == UNKNOWN


# --- a record without procStart is not trusted ---------------------------------


def _bare_record(home, pid, **fields):
    (home.sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, **fields}))


def test_an_interactive_record_without_procstart_grants_nothing_but_adds_no_restriction(home):
    _bare_record(home, ME, kind="interactive")
    assert mode_of(ME) is None


def test_a_record_without_procstart_falls_through_to_the_ancestors(home):
    _restricted_parent(home)
    _bare_record(home, ME, kind="interactive")
    assert mode_of(ME) == "delegate"


def test_a_bg_record_without_procstart_and_no_other_evidence_is_unknown(home):
    _bare_record(home, ME, kind="bg", jobId=JOB)
    assert mode_of(ME) == UNKNOWN


def test_a_parked_record_without_procstart_and_no_other_evidence_is_unknown(home):
    _bare_record(home, ME, kind="interactive", parkedJobId=JOB)
    assert mode_of(ME) == UNKNOWN


def test_a_bg_record_without_procstart_is_judged_by_the_roster(home):
    _bare_record(home, ME, kind="bg", jobId=JOB)
    home.worker()
    home.lineage("secretary")
    assert mode_of(ME) == "secretary"


def test_an_empty_procstart_is_treated_as_missing(home):
    _bare_record(home, ME, kind="bg", jobId=JOB, procStart="")
    assert mode_of(ME) == UNKNOWN


def test_a_mismatched_procstart_is_still_stale(home):
    home.record(ME, "interactive", proc_start="1")
    assert mode_of(ME) == UNKNOWN
