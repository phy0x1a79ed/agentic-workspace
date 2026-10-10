"""start, list and stop: who may start a session, what it launches with, and
what the lineage record lets `stop` and `mode_of` do.

The launcher and the scopes call are mocked. The roster is a temporary file
whose workers point at real live pids (the test process and its parent), since
liveness is a pid plus that process's start time.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

PARENT_JOB = "aaaaaaaa"
CHILD_JOB = "bbbbbbbb"


def _proc_start(pid: int) -> str:
    with open(f"/proc/{pid}/stat") as fh:
        return fh.read().partition(") ")[2].split()[19]


def _worker(pid: int, seed_name: str, *, started_ms: int = 1_700_000_000_000) -> dict:
    return {
        "replPid": pid, "replProcStart": _proc_start(pid), "startedAt": started_ms,
        "cliVersion": "2.1.268", "sessionId": f"sid-{pid}",
        "dispatch": {"source": "shell", "seed": {"name": seed_name}},
    }


class Home:
    """A temporary Claude home with a live roster the tests can add sessions to."""

    def __init__(self, root):
        self.root = root
        self.roster = root / "daemon" / "roster.json"
        self.jobs = root / "jobs"
        self.projects = root / "projects"
        self.sessions = root / "sessions"
        self.roster.parent.mkdir(parents=True)
        self.set_workers({})

    def set_workers(self, workers: dict, supervisor: int | None = None) -> None:
        sup = os.getpid() if supervisor is None else supervisor
        self.roster.write_text(json.dumps({"supervisorPid": sup, "workers": workers}))

    def workers(self) -> dict:
        return json.loads(self.roster.read_text())["workers"]

    def add(self, short: str, name: str, pid: int, *, cwd: str = "/home/tony",
            state: str = "working", tokens: int = 0, intent: str = "",
            started_ms: int = 1_700_000_000_000) -> None:
        workers = self.workers()
        workers[short] = _worker(pid, name, started_ms=started_ms)
        self.set_workers(workers)
        (self.jobs / short).mkdir(parents=True, exist_ok=True)
        (self.jobs / short / "state.json").write_text(json.dumps({
            "name": name, "tokens": tokens, "intent": intent, "cwd": cwd,
            "state": state, "needs": None}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = Home(tmp_path / "claude")
    (h.projects / "awm" / "demo").mkdir(parents=True)
    trust = tmp_path / "claude.json"
    trust.write_text(json.dumps({"projects": {str(tmp_path): {
        "hasTrustDialogAccepted": True}}}))
    monkeypatch.setenv("AWM_CX_ROSTER", str(h.roster))
    monkeypatch.setenv("AWM_CX_JOBS", str(h.jobs))
    monkeypatch.setenv("AWM_CX_STATE", str(h.root / "cx"))
    monkeypatch.setenv("AWM_CX_SESSIONS", str(h.sessions))
    monkeypatch.setenv("AWM_CX_PROJECTS", str(h.projects))
    monkeypatch.setenv("AWM_CX_TRUST_FILE", str(trust))
    monkeypatch.delenv("AWM_NODE_ROLE", raising=False)
    monkeypatch.setenv("AWM_NODE_NAME", "testnode")
    return h


@pytest.fixture
def launched(home, monkeypatch):
    """Replace the launcher with one that records its call and registers a
    session under the child's pid, the way the daemon would."""
    from awm.cx import lifecycle, sessions

    calls: list[dict] = []

    async def fake(*, cwd, name, flags, prompt):
        calls.append({"cwd": cwd, "name": name, "flags": flags, "prompt": prompt})
        home.add(CHILD_JOB, name, os.getppid(), cwd=str(cwd))
        return next(s for s in sessions.load() if s.short == CHILD_JOB)

    monkeypatch.setattr(lifecycle, "launch_session", fake)
    return calls


@pytest.fixture
def peers(monkeypatch):
    from awm.cx import lifecycle

    book = {"capella": "domestic", "john": "foreign"}
    monkeypatch.setattr(lifecycle, "peer_relation", lambda name: book.get(name))
    return book


def start(args, as_=None):
    from awm.cx import lifecycle

    return asyncio.run(lifecycle.start(args, as_))


def stop(args, as_=None):
    from awm.cx import lifecycle

    return asyncio.run(lifecycle.stop(args, as_))


BASE = {"project": "awm", "scope": "demo"}


# --- a successful start ------------------------------------------------------


def test_start_returns_the_contract_and_writes_lineage(home, launched):
    home.add(PARENT_JOB, "caller", os.getpid())
    out = start({**BASE, "prompt": "do it", "mode": "rep", "_caller_pid": os.getpid()})
    assert out == {
        "ok": True, "job": CHILD_JOB, "name": "demo", "node": "testnode",
        "project": "awm", "scope": "demo", "cwd": str(home.projects / "awm" / "demo"),
        "attach": f"claude attach {CHILD_JOB}", "parent": PARENT_JOB, "mode": "rep",
    }
    from awm.cx import lifecycle

    rec = lifecycle.read_lineage(CHILD_JOB)
    assert rec["parent"] == PARENT_JOB and rec["caller"] == "local"
    assert rec["mode"] == "rep" and rec["project"] == "awm" and rec["scope"] == "demo"
    assert rec["remote_control"] is None and rec["started_at"]
    assert launched[0]["prompt"] == "do it"


def test_defaults_are_skip_permissions_sonnet_and_medium(home, launched):
    start(BASE)
    flags = launched[0]["flags"]
    assert "--dangerously-skip-permissions" in flags
    assert flags[-4:] == ["--effort", "medium", "--model", "sonnet[1m]"]
    assert "--permission-mode" not in flags and "--remote-control" not in flags


def test_arguments_override_the_defaults(home, launched):
    start({**BASE, "name": "worker one", "model": "opus", "effort": "high",
           "permission": "plan", "disallowed_tools": ["Write", "Edit"],
           "remote_control": True})
    call = launched[0]
    assert call["name"] == "worker one"
    flags = call["flags"]
    assert flags[:2] == ["--permission-mode", "plan"]
    assert "--dangerously-skip-permissions" not in flags
    assert flags[flags.index("--disallowedTools") + 1] == "Write,Edit"
    assert flags[flags.index("--remote-control") + 1] == "worker one"
    assert flags[-4:] == ["--effort", "high", "--model", "opus"]


def test_the_model_is_a_flag_and_not_an_environment_variable(home, launched):
    """A bg session gets its model from `--model`; the flag wins over the env."""
    import inspect

    from awm.cx import lifecycle

    assert "env" not in inspect.signature(lifecycle.launch_session).parameters


def test_a_model_supplied_parent_is_ignored(home, launched):
    out = start({**BASE, "parent": "evil", "caller": "evil"})
    assert out["ok"] and out["parent"] is None


def test_a_started_session_is_not_a_pool_session(home, launched):
    from awm.cx import config, sessions

    start(BASE)
    s = next(s for s in sessions.load() if s.short == CHILD_JOB)
    assert not s.name.startswith(config.name_prefix())
    assert not sessions.is_ours(s) and not sessions.was_ours(s)
    assert not sessions.removable(s, version=None)


def test_a_missing_worktree_is_created_once(home, launched, monkeypatch):
    from awm.cx import lifecycle

    made = []

    async def fake_create(project, scope):
        made.append((project, scope))
        (home.projects / project / scope).mkdir(parents=True)

    monkeypatch.setattr(lifecycle, "create_scope", fake_create)
    assert start({"project": "awm", "scope": "fresh"})["ok"]
    assert made == [("awm", "fresh")]
    start({**BASE, "name": "another"})
    assert made == [("awm", "fresh")], "an existing worktree is not created again"


def test_a_failed_scope_create_is_a_refusal(home, launched, monkeypatch):
    from awm.cx import lifecycle

    async def boom(project, scope):
        raise RuntimeError("no such project")

    monkeypatch.setattr(lifecycle, "create_scope", boom)
    out = start({"project": "nope", "scope": "x"})
    assert out["ok"] is False and "no such project" in out["reason"]
    assert launched == []


# --- refusals ----------------------------------------------------------------


def test_a_station_refuses(home, launched, monkeypatch):
    monkeypatch.setenv("AWM_NODE_ROLE", "station")
    out = start(BASE)
    assert out["ok"] is False and "station" in out["reason"]
    assert launched == []


def test_the_legacy_bare_peer_is_refused(home, launched, peers):
    out = start(BASE, "peer")
    assert out["ok"] is False and "peer" in out["reason"]
    assert launched == []


def test_a_foreign_peer_is_refused(home, launched, peers):
    out = start(BASE, "peer:john")
    assert out["ok"] is False and "foreign" in out["reason"]
    assert launched == []


def test_a_peer_outside_the_book_is_refused(home, launched, peers):
    out = start(BASE, "peer:stranger")
    assert out["ok"] is False and launched == []


def test_a_domestic_peer_may_start_and_is_recorded_as_the_caller(home, launched, peers):
    out = start(BASE, "peer:capella")
    assert out["ok"] is True
    from awm.cx import lifecycle

    assert lifecycle.read_lineage(CHILD_JOB)["caller"] == "capella"


def test_no_daemon_refuses(home, launched):
    home.set_workers({}, supervisor=2 ** 22 + 12345)
    out = start(BASE)
    assert out["ok"] is False and "daemon" in out["reason"]
    assert launched == []


def test_an_untrusted_worktree_refuses(home, launched, tmp_path, monkeypatch):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"projects": {}}))
    monkeypatch.setenv("AWM_CX_TRUST_FILE", str(other))
    out = start(BASE)
    assert out["ok"] is False and "trust" in out["reason"]
    assert launched == []


def test_a_taken_name_refuses(home, launched):
    home.add(PARENT_JOB, "demo", os.getpid())
    out = start(BASE)
    assert out["ok"] is False and "named" in out["reason"]
    assert launched == []


def test_the_pool_prefix_is_not_a_name(home, launched):
    out = start({**BASE, "name": "<warm sneaky>"})
    assert out["ok"] is False and launched == []


@pytest.mark.parametrize("args", [
    {"project": "awm", "scope": "../escape"},
    {"project": "..", "scope": "demo"},
    {"project": "awm", "scope": "/abs"},
    {"project": "awm", "scope": "demo", "effort": "ludicrous"},
    {"project": "awm", "scope": "demo", "permission": "yolo"},
    {"project": "awm", "scope": "demo", "model": "--evil"},
    {"project": "awm", "scope": "demo", "mode": "has space"},
    {"scope": "demo"},
])
def test_bad_arguments_refuse(home, launched, args):
    out = start(args)
    assert out["ok"] is False and launched == []


def test_a_launch_that_never_registers_is_a_refusal(home, monkeypatch):
    from awm.cx import lifecycle

    async def never(**kw):
        raise TimeoutError("no session named 'demo' appeared")

    monkeypatch.setattr(lifecycle, "launch_session", never)
    out = start(BASE)
    assert out["ok"] is False and "did not start" in out["reason"]
    assert lifecycle.read_lineage(CHILD_JOB) is None


# --- stop --------------------------------------------------------------------


@pytest.fixture
def stops(monkeypatch):
    from awm.cx import lifecycle

    calls: list[str] = []

    async def fake(job):
        calls.append(job)
        return True, ""

    monkeypatch.setattr(lifecycle, "run_claude_stop", fake)
    return calls


def test_stop_refuses_a_job_without_lineage(home, stops):
    home.add("cccccccc", "hand started", os.getpid())
    out = stop({"job": "cccccccc"})
    assert out["ok"] is False and "not started by cx" in out["reason"]
    assert stops == []


def test_stop_ends_a_job_start_created(home, launched, stops):
    start(BASE)
    assert stop({"job": CHILD_JOB}) == {"ok": True, "job": CHILD_JOB}
    assert stops == [CHILD_JOB]


def test_stop_reports_a_failed_claude_stop(home, launched, monkeypatch):
    from awm.cx import lifecycle

    async def fail(job):
        return False, "no such job"

    monkeypatch.setattr(lifecycle, "run_claude_stop", fail)
    start(BASE)
    out = stop({"job": CHILD_JOB})
    assert out["ok"] is False and out["reason"] == "no such job"


@pytest.mark.parametrize("job", ["", "../x", "ZZZZZZZZ", None, 7, "aaaaaaaaa"])
def test_stop_rejects_a_malformed_job(home, stops, job):
    assert stop({"job": job})["ok"] is False
    assert stops == []


def test_a_foreign_peer_cannot_stop(home, launched, stops, peers):
    start(BASE)
    assert stop({"job": CHILD_JOB}, "peer:john")["ok"] is False
    assert stop({"job": CHILD_JOB}, "peer")["ok"] is False
    assert stops == []


def test_stop_never_deletes_the_conversation():
    """`claude stop` keeps the conversation; `claude rm` would not."""
    import inspect

    from awm.cx import lifecycle

    src = inspect.getsource(lifecycle.run_claude_stop)
    assert '"stop"' in src and '"rm"' not in src


# --- the reconcile prune -----------------------------------------------------


def test_prune_drops_lineage_of_a_job_the_roster_lost(home, launched):
    from awm.cx import config, lifecycle

    start(BASE)
    path = config.starts_dir() / f"{CHILD_JOB}.json"
    old = time.time() - 3600
    os.utime(path, (old, old))
    home.set_workers({})
    assert lifecycle.prune_lineage() == [CHILD_JOB]
    assert not path.exists()


def test_prune_keeps_lineage_of_a_held_job(home, launched):
    from awm.cx import config, lifecycle

    start(BASE)
    path = config.starts_dir() / f"{CHILD_JOB}.json"
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert lifecycle.prune_lineage() == []
    assert path.exists()


def test_prune_leaves_a_fresh_record_alone(home, launched):
    from awm.cx import config, lifecycle

    start(BASE)
    home.set_workers({})
    assert lifecycle.prune_lineage() == []
    assert (config.starts_dir() / f"{CHILD_JOB}.json").exists()


def test_prune_does_nothing_when_no_daemon_runs(home, launched):
    """With no daemon the roster reads empty, and every record would look orphaned."""
    from awm.cx import config, lifecycle

    start(BASE)
    path = config.starts_dir() / f"{CHILD_JOB}.json"
    old = time.time() - 3600
    os.utime(path, (old, old))
    home.set_workers({}, supervisor=2 ** 22 + 12345)
    assert lifecycle.prune_lineage() == []
    assert path.exists()


async def test_the_reconcile_tick_prunes(home, launched, monkeypatch):
    from awm.cx import config, lifecycle, reconcile, remove, seed

    assert (await lifecycle.start(BASE))["ok"]
    path = config.starts_dir() / f"{CHILD_JOB}.json"
    old = time.time() - 3600
    os.utime(path, (old, old))
    home.set_workers({})
    monkeypatch.setenv("AWM_CX_WANT", "0")

    async def nothing(now=None):
        return []

    monkeypatch.setattr(remove, "apply", nothing)
    monkeypatch.setattr(seed, "seed_one", lambda: None)
    await reconcile.Loop().tick()
    assert not path.exists()


# --- mode_of -----------------------------------------------------------------


def test_mode_of_reads_the_mode_through_the_roster(home, launched):
    from awm.cx import lifecycle

    start({**BASE, "mode": "representative"})
    assert lifecycle.mode_of(os.getppid()) == "representative"


def test_mode_of_is_none_for_everything_else(home, launched):
    from awm.cx import lifecycle

    start(BASE)
    home.add("dddddddd", "<warm otter>", os.getpid())
    assert lifecycle.mode_of(os.getpid()) is None, "a pool session has no lineage"
    assert lifecycle.mode_of(1) is None
    for bad in (None, 0, -3, True, "12"):
        assert lifecycle.mode_of(bad) is None


# --- list --------------------------------------------------------------------


def test_list_merges_roster_lineage_and_interactive_sessions(home, launched, monkeypatch):
    from awm.cx import lifecycle

    home.add(PARENT_JOB, "<warm saiga>", os.getpid(), started_ms=1_600_000_000_000)
    start({**BASE, "mode": "rep"})
    home.sessions.mkdir(parents=True)
    me = os.getpid()
    (home.sessions / f"{me}.json").write_text(json.dumps({
        "pid": me, "procStart": _proc_start(me), "kind": "interactive",
        "name": "dev", "cwd": str(home.projects / "awm" / "demo"), "status": "idle",
        "startedAt": 1_650_000_000_000}))
    monkeypatch.setattr(lifecycle, "_tmux_panes", lambda: {me: "main"})

    rows = {(r["job"] or r["tmux"]): r for r in lifecycle.collect()}
    pool_row = rows[PARENT_JOB]
    assert pool_row["pool"] is True and pool_row["mode"] is None
    child = rows[CHILD_JOB]
    assert child["pool"] is False and child["mode"] == "rep"
    assert child["parent"] is None and child["caller"] == "local"
    assert child["project"] == "awm" and child["scope"] == "demo"
    assert child["attach"] == f"claude attach {CHILD_JOB}" and child["node"] == "testnode"
    tmux = rows["main"]
    assert tmux["attach"] == "tmux attach -t main" and tmux["job"] is None
    assert tmux["name"] == "dev" and tmux["scope"] == "demo"
    assert list(rows) == [PARENT_JOB, "main", CHILD_JOB], "oldest first"


def test_list_filters_by_project_and_scope(home, launched, monkeypatch):
    from awm.cx import lifecycle

    monkeypatch.setattr(lifecycle, "_tmux_panes", dict)
    home.add(PARENT_JOB, "<warm saiga>", os.getpid())
    start(BASE)
    assert [r["job"] for r in lifecycle.collect(project="awm")] == [CHILD_JOB]
    assert [r["job"] for r in lifecycle.collect(scope="demo")] == [CHILD_JOB]
    assert lifecycle.collect(project="other") == []


async def test_the_list_verb_keeps_the_pool_summary(home, launched):
    from awm.cx import hub_adapter

    out = await hub_adapter.HANDLERS["list"]({})
    assert set(out) == {"sessions", "pool"}
    assert {"warm", "want", "daemon_pid", "loop"} <= set(out["pool"])


# --- the launch command ------------------------------------------------------


def test_launch_argv_sets_the_directory_and_survives_the_unit(monkeypatch, tmp_path):
    from awm.cx import lifecycle

    monkeypatch.setattr(lifecycle, "_user_manager_env", lambda: {"XDG_RUNTIME_DIR": "/r"})
    argv, env = lifecycle.launch_argv(tmp_path, "n", ["--model", "m"], "the task")
    assert argv[:2] == ["systemd-run", "--user"]
    assert "--property=KillMode=process" in argv
    assert f"--working-directory={tmp_path}" in argv
    assert argv[-2:] == ["--", "the task"], "the prompt follows a bare --"
    from awm.cx import config

    assert argv[argv.index("--bg") - 1] == config.claude_bin()
    assert env == {"XDG_RUNTIME_DIR": "/r"}


def test_launch_argv_without_a_prompt_has_no_separator(monkeypatch, tmp_path):
    from awm.cx import lifecycle

    monkeypatch.setattr(lifecycle, "_user_manager_env", lambda: None)
    argv, env = lifecycle.launch_argv(tmp_path, "n", ["--model", "m"], None)
    assert "--" not in argv and argv[1:4] == ["--bg", "-n", "n"] and env == {}


async def test_launch_session_waits_for_the_named_session(home, monkeypatch, tmp_path):
    from awm.cx import lifecycle

    seen = {}

    class Proc:
        async def wait(self):
            home.add(CHILD_JOB, "demo", os.getppid())
            return 0

        def kill(self):
            pass

    async def fake_exec(*argv, **kw):
        seen.update(argv=argv, cwd=kw["cwd"])
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(lifecycle, "_user_manager_env", lambda: None)
    s = await lifecycle.launch_session(cwd=tmp_path, name="demo", flags=[], prompt="p")
    assert s.short == CHILD_JOB and seen["cwd"] == str(tmp_path)


async def test_launch_session_times_out_without_a_new_session(home, monkeypatch, tmp_path):
    from awm.cx import lifecycle

    class Proc:
        async def wait(self):
            return 1

        def kill(self):
            pass

    async def fake_exec(*argv, **kw):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(lifecycle, "_user_manager_env", lambda: None)
    monkeypatch.setattr(lifecycle, "LAUNCH_TIMEOUT_S", 0.3)
    with pytest.raises(TimeoutError):
        await lifecycle.launch_session(cwd=tmp_path, name="demo", flags=[], prompt=None)


# --- the manifest ------------------------------------------------------------


def test_the_manifest_declares_effects_and_drops_status():
    from awm.cx.hub_adapter import API_MANIFEST

    fns = {f["name"]: f for f in API_MANIFEST["functions"]}
    assert {"start", "list", "stop", "claim", "seed", "remove"} == set(fns)
    assert "status" not in fns and "wake" not in fns
    assert fns["start"]["effect"] == "write" and fns["stop"]["effect"] == "write"
    assert fns["list"]["effect"] == "read"
    assert all(f["effect"] in ("read", "write") for f in fns.values())
    assert "tier" not in str(API_MANIFEST)
