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
        self.jobs.mkdir()
        self.sessions.mkdir()
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


    def record(self, pid: int, kind: str, job: str | None = None,
               name: str = "x") -> None:
        """Claude Code's per-process record for a live pid."""
        (self.sessions / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "procStart": _proc_start(pid), "kind": kind,
            "jobId": job, "name": name, "sessionId": f"sid-{pid}", "cwd": "/home/tony",
            "status": "idle", "startedAt": 1_650_000_000_000}))


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
    h.mcp_source = tmp_path / "workspace.mcp.json"
    h.mcp_source.write_text(json.dumps({"mcpServers": {
        "awm": {"command": "/opt/awm/bin/awm-mcp", "args": [],
                "env": {"AWM_WORKSPACE": "/ws"}, "extra": "dropped"},
        "other": {"command": "npx"}}}))
    monkeypatch.setenv("AWM_CX_MCP_SOURCE", str(h.mcp_source))
    monkeypatch.delenv("AWM_NODE_ROLE", raising=False)
    monkeypatch.setenv("AWM_NODE_NAME", "testnode")
    return h


@pytest.fixture
def launched(home, monkeypatch):
    """Replace the shared launcher with one that records its call and registers
    a session under the child's pid, the way the daemon would."""
    from awm.cx import lifecycle
    from awm.claudedaemon import roster

    calls: list[dict] = []

    async def fake(**kw):
        calls.append(kw)
        from awm.cx import config

        pending = [p for p in os.listdir(config.starts_dir())
                   if p.startswith("pending-")]
        kw["seen_pending"] = pending
        home.add(CHILD_JOB, kw["name"], os.getppid(), cwd=str(kw["cwd"]))
        return next(s for s in roster.load(kw["roster_path"], kw["jobs_dir"])
                    if s.short == CHILD_JOB)

    monkeypatch.setattr(lifecycle.launch, "launch", fake)
    return calls


def lineage(job, **over):
    from awm.cx import lifecycle

    lifecycle.ensure_starts_dir()
    rec = {"job": job, "name": job, "project": "awm", "scope": "demo", "cwd": "/x",
           "parent": None, "caller": "local", "mode": "worker",
           "remote_control": None, "started_at": "2026-01-01T00:00:00+00:00",
           **over}
    lifecycle._write_lineage(job, rec)
    return rec


def parent_session(home, mode=None):
    """The test process as a background session, optionally cx-started."""
    home.add(PARENT_JOB, "caller", os.getpid())
    home.record(os.getpid(), "bg", PARENT_JOB, "caller")
    if mode:
        lineage(PARENT_JOB, mode=mode, name="caller")


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
    parent_session(home)
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


def test_start_calls_the_shared_launcher_with_cx_settings(home, launched):
    from awm.cx import config

    start(BASE)
    call = launched[0]
    assert call["name"] == "demo" and call["cwd"] == home.projects / "awm" / "demo"
    assert call["env"] == {}, "the model is a flag, not ANTHROPIC_MODEL"
    assert call["unit_prefix"] == "awm-cx-start"
    assert call["claude"] == config.claude_bin()
    assert call["roster_path"] == config.roster_path()
    assert call["jobs_dir"] == config.jobs_dir()
    assert callable(call["accept"])


def test_the_accept_filter_takes_only_the_named_session(home, launched):
    start(BASE)
    accept = launched[0]["accept"]

    class S:
        def __init__(self, seed_name, name):
            self.seed_name, self.name = seed_name, name

    assert accept(S("demo", "")) and accept(S(None, "demo"))
    assert not accept(S("other", "other"))


def test_defaults_are_skip_permissions_sonnet_and_medium(home, launched):
    start(BASE)
    flags = launched[0]["flags"]
    assert "--dangerously-skip-permissions" in flags
    assert flags[-2:] == ["--effort=medium", "--model=sonnet[1m]"]
    assert not any(f.startswith(("--permission-mode", "--remote-control")) for f in flags)


def test_arguments_override_the_defaults_in_equals_form(home, launched):
    start({**BASE, "name": "worker one", "model": "opus", "effort": "high",
           "permission": "plan", "disallowed_tools": ["Write", "Edit"],
           "remote_control": True})
    call = launched[0]
    assert call["name"] == "worker one"
    assert call["flags"] == ["--permission-mode=plan", "--disallowedTools=Write,Edit",
                             "--remote-control=worker-one", "--effort=high",
                             "--model=opus"]


def test_allowed_tools_become_one_equals_form_flag(home, launched):
    start({**BASE, "permission": "dontAsk", "disallowed_tools": ["Bash"],
           "allowed_tools": ["mcp__awm__*", "SendMessage"], "remote_control": "rc1"})
    flags = launched[0]["flags"]
    assert flags == ["--permission-mode=dontAsk", "--disallowedTools=Bash",
                     "--allowedTools=mcp__awm__*,SendMessage", "--remote-control=rc1",
                     "--effort=medium", "--model=sonnet[1m]"]
    assert "--allowedTools" not in flags


def test_allowed_tools_may_be_a_comma_string_and_default_to_none(home, launched):
    start({**BASE, "allowed_tools": "Read,Skill"})
    assert "--allowedTools=Read,Skill" in launched[0]["flags"]
    start({**BASE, "name": "second"})
    assert not any(f.startswith("--allowedTools") for f in launched[1]["flags"])


def test_allowed_tools_must_be_a_list_of_names(home, launched):
    out = start({**BASE, "allowed_tools": [1, 2]})
    assert not out["ok"] and "allowed_tools" in out["reason"]


def test_every_valued_flag_uses_the_equals_form(home, launched):
    start({**BASE, "permission": "auto", "disallowed_tools": "Bash", "remote_control": "rc1"})
    for flag in launched[0]["flags"]:
        assert flag.startswith("--") and (flag.count("=") == 1 or "=" not in flag)
    bare = {"--effort", "--model", "--permission-mode", "--disallowedTools",
            "--remote-control"}
    assert not bare & set(launched[0]["flags"])


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


def test_the_shared_launcher_rechecks_the_daemon_and_its_refusal_is_mapped(
        home, monkeypatch):
    """The daemon can die between cx's check and the launch."""
    from awm.cx import lifecycle

    async def refuse(**kw):
        raise lifecycle.launch.Refused("no claude code daemon is running")

    monkeypatch.setattr(lifecycle.launch, "launch", refuse)
    out = start(BASE)
    assert out["ok"] is False and "daemon" in out["reason"]
    from awm.cx import config

    assert not [p for p in os.listdir(config.starts_dir()) if p.startswith("pending-")], \
        "a launch that never ran leaves no pending record"


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


@pytest.mark.parametrize("name", [
    "<warm sneaky>", " <warm sneaky>", "\t<warm x>", "a<warm", "<WARM x>", "x <Warm y>",
])
def test_no_spelling_of_the_pool_prefix_is_a_name(home, launched, name):
    out = start({**BASE, "name": name})
    assert out["ok"] is False and launched == []


def test_the_name_is_stripped_before_it_is_used(home, launched):
    out = start({**BASE, "name": "  tidy  "})
    assert out["ok"] and out["name"] == "tidy" and launched[0]["name"] == "tidy"


@pytest.mark.parametrize("name", ["-x", "--evil", " -x"])
def test_a_leading_dash_is_not_a_name(home, launched, name):
    assert start({**BASE, "name": name})["ok"] is False and launched == []


@pytest.mark.parametrize("rc", ["--evil", "-x", "a b", "x" * 65, "", " ", "a;b", 5])
def test_remote_control_cannot_inject_flags(home, launched, rc):
    out = start({**BASE, "remote_control": rc})
    if rc in ("", " "):
        return  # an empty value is "off" or refused; either way nothing is injected
    assert out["ok"] is False and launched == []


def test_remote_control_accepts_a_plain_label(home, launched):
    assert start({**BASE, "remote_control": "rc-1.a"})["ok"]
    assert "--remote-control=rc-1.a" in launched[0]["flags"]


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


# --- a launch that outlives its timeout (M3) ---------------------------------


def _pendings():
    from awm.cx import config

    return sorted(p for p in os.listdir(config.starts_dir()) if p.startswith("pending-"))


def test_the_pending_record_exists_during_the_launch_and_is_replaced_after(home, launched):
    out = start({**BASE, "mode": "representative"})
    assert len(launched[0]["seen_pending"]) == 1, "written before the launch"
    assert _pendings() == [], "dropped once the job record is written"
    from awm.cx import lifecycle

    assert lifecycle.read_lineage(out["job"])["mode"] == "representative"


@pytest.mark.parametrize("exc", [TimeoutError("none appeared"), OSError("exec failed")])
def test_a_failed_launch_leaves_the_pending_record(home, monkeypatch, exc):
    from awm.cx import lifecycle

    async def fail(**kw):
        raise exc

    monkeypatch.setattr(lifecycle.launch, "launch", fail)
    out = start({**BASE, "mode": "representative"})
    assert out["ok"] is False and "did not start" in out["reason"]
    assert len(_pendings()) == 1


def test_an_oserror_from_the_start_path_is_a_refusal_not_a_crash(home, monkeypatch):
    from awm.cx import lifecycle

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(lifecycle, "_resolve_worktree", lambda *a, **k: boom())
    out = start(BASE)
    assert out["ok"] is False and "disk gone" in out["reason"]


def test_a_pending_record_that_cannot_be_written_stops_the_launch(home, launched,
                                                                  tmp_path, monkeypatch):
    blocker = tmp_path / "state-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("AWM_CX_STATE", str(blocker))
    out = start(BASE)
    assert out["ok"] is False and "record" in out["reason"]
    assert launched == []


def test_a_pending_start_blocks_the_same_name(home, launched, monkeypatch):
    from awm.cx import lifecycle

    async def fail(**kw):
        raise TimeoutError("late")

    monkeypatch.setattr(lifecycle.launch, "launch", fail)
    start(BASE)
    out = start(BASE)
    assert out["ok"] is False and "named" in out["reason"]


def test_mode_of_applies_the_pending_mode_to_a_late_job(home, monkeypatch):
    from awm.cx import lifecycle

    async def fail(**kw):
        raise TimeoutError("late")

    monkeypatch.setattr(lifecycle.launch, "launch", fail)
    start({**BASE, "mode": "representative"})
    home.add(CHILD_JOB, "demo", os.getppid())
    assert lifecycle.mode_of(os.getppid()) == "representative"


def test_adopt_pending_gives_the_late_job_its_record(home, monkeypatch):
    from awm.cx import lifecycle

    async def fail(**kw):
        raise TimeoutError("late")

    monkeypatch.setattr(lifecycle.launch, "launch", fail)
    start({**BASE, "mode": "representative"})
    assert lifecycle.adopt_pending() == [], "no job has appeared yet"
    home.add(CHILD_JOB, "demo", os.getppid(), started_ms=int(time.time() * 1000))
    assert lifecycle.adopt_pending() == [CHILD_JOB]
    assert _pendings() == []
    assert lifecycle.read_lineage(CHILD_JOB)["mode"] == "representative"
    assert lifecycle.mode_of(os.getppid()) == "representative"


def test_adopt_pending_ignores_a_session_older_than_the_start(home, monkeypatch):
    from awm.cx import lifecycle

    async def fail(**kw):
        raise TimeoutError("late")

    monkeypatch.setattr(lifecycle.launch, "launch", fail)
    start(BASE)
    home.add(CHILD_JOB, "demo", os.getppid(), started_ms=1_000_000_000_000)
    assert lifecycle.adopt_pending() == [] and len(_pendings()) == 1


def test_an_expired_pending_record_is_dropped(home, monkeypatch):
    from awm.cx import lifecycle

    async def fail(**kw):
        raise TimeoutError("never")

    monkeypatch.setattr(lifecycle.launch, "launch", fail)
    start(BASE)
    assert lifecycle.adopt_pending(now=time.time() + 10) == [] and len(_pendings()) == 1
    lifecycle.adopt_pending(now=time.time() + lifecycle.PENDING_TTL_S + 60)
    assert _pendings() == []


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


def test_a_moded_session_stops_only_its_own_children(home, stops):
    parent_session(home, mode="representative")
    lineage(CHILD_JOB, parent="zzzzzzzz")
    out = stop({"job": CHILD_JOB, "_caller_pid": os.getpid()})
    assert out["ok"] is False and "started" in out["reason"]
    lineage(CHILD_JOB, parent=PARENT_JOB)
    assert stop({"job": CHILD_JOB, "_caller_pid": os.getpid()})["ok"] is True
    assert stops == [CHILD_JOB]


def test_a_worker_session_also_stops_only_its_own_children(home, stops):
    parent_session(home, mode="worker")
    lineage(CHILD_JOB, parent="zzzzzzzz")
    assert stop({"job": CHILD_JOB, "_caller_pid": os.getpid()})["ok"] is False


def test_a_caller_with_no_cx_mode_may_stop_any_cx_job(home, stops):
    lineage(CHILD_JOB, parent="zzzzzzzz")
    assert stop({"job": CHILD_JOB})["ok"] is True, "an operator or a service"
    home.add("dddddddd", "<warm otter>", os.getpid())
    home.record(os.getpid(), "bg", "dddddddd")
    assert stop({"job": CHILD_JOB, "_caller_pid": os.getpid()})["ok"] is True


def test_stop_refuses_a_caller_whose_mode_is_unknown(home, stops):
    lineage(CHILD_JOB)
    home.roster.write_text("{ not json")
    out = stop({"job": CHILD_JOB, "_caller_pid": os.getpid()})
    assert out["ok"] is False and "determined" in out["reason"]
    assert stops == []


def test_stop_never_deletes_the_conversation():
    """`claude stop` keeps the conversation; `claude rm` would not."""
    import inspect

    from awm.cx import lifecycle

    src = inspect.getsource(lifecycle.run_claude_stop)
    assert '"stop"' in src and '"rm"' not in src


# --- the caller's mode gates what it may start -------------------------------


def allowed_list(flags):
    return next(f for f in flags if f.startswith("--allowedTools=")).split("=", 1)[1].split(",")


@pytest.mark.parametrize("caller_mode", ["representative", "secretary", "delegate"])
def test_a_gated_caller_gets_a_delegate_with_a_fixed_policy(home, launched, caller_mode):
    parent_session(home, mode=caller_mode)
    ok = start({**BASE, "model": "opus", "effort": "high", "prompt": "do it",
                "_caller_pid": os.getpid()})
    assert ok["ok"] and ok["mode"] == "delegate" and ok["parent"] == PARENT_JOB
    flags = launched[0]["flags"]
    assert "--permission-mode=dontAsk" in flags and "--restricted" in flags
    assert "--strict-mcp-config" in flags
    tools = next(f for f in flags if f.startswith("--tools="))
    assert "Bash" not in tools.split("=", 1)[1].split(",")
    assert "Edit" in tools and "Task" in tools
    assert "SendMessage" not in tools and "ListAgents" not in tools
    assert not any(t in allowed_list(flags) for t in ("SendMessage", "ListAgents"))
    allowed = next(f for f in flags if f.startswith("--allowedTools="))
    assert "mcp__awm__*" in allowed and "Bash" not in allowed.split(",")
    assert not any(f.startswith(("--remote-control", "--dangerously")) for f in flags)
    assert "--model=opus" in flags and "--effort=high" in flags


def test_the_delegate_loads_only_the_awm_server_from_the_workspace_config(home, launched):
    parent_session(home, mode="representative")
    start({**BASE, "_caller_pid": os.getpid()})
    flags = launched[0]["flags"]
    path = next(f for f in flags if f.startswith("--mcp-config=")).split("=", 1)[1]
    assert json.loads(open(path).read()) == {"mcpServers": {"awm": {
        "command": "/opt/awm/bin/awm-mcp", "args": [],
        "env": {"AWM_WORKSPACE": "/ws"}}}}


@pytest.mark.parametrize("override", [
    {"permission": "bypassPermissions"}, {"permission": "plan"},
    {"allowed_tools": ["Bash"]}, {"disallowed_tools": ["Read"]}, {"tools": ["Bash"]},
    {"remote_control": True}, {"remote_control": "x"}, {"mode": "worker"},
    {"restricted": False}, {"strict_mcp": False}, {"model": "opus-evil"},
])
def test_a_gated_caller_may_not_override_the_policy(home, launched, override):
    parent_session(home, mode="representative")
    out = start({**BASE, "_caller_pid": os.getpid(), **override})
    assert out["ok"] is False and launched == []


def test_a_gated_caller_may_not_create_a_scope(home, launched, monkeypatch):
    from awm.cx import lifecycle

    async def boom(*a, **k):
        raise AssertionError("created a scope")

    monkeypatch.setattr(lifecycle, "create_scope", boom)
    parent_session(home, mode="secretary")
    out = start({"project": "awm", "scope": "nowhere", "_caller_pid": os.getpid()})
    assert out["ok"] is False and "may not create scopes" in out["reason"]
    assert launched == []


def test_a_missing_awm_mcp_server_refuses_a_delegate(home, launched):
    parent_session(home, mode="representative")
    home.mcp_source.write_text(json.dumps({"mcpServers": {}}))
    out = start({**BASE, "_caller_pid": os.getpid()})
    assert out["ok"] is False and "awm server" in out["reason"]


def test_the_front_door_with_no_session_may_start_the_reserved_sessions(home, launched):
    out = start({**BASE, "name": "representative", "mode": "representative",
                 "permission": "dontAsk", "tools": ["SendMessage"],
                 "restricted": True, "strict_mcp": True})
    assert out["ok"] and out["mode"] == "representative"
    flags = launched[0]["flags"]
    assert "--tools=SendMessage" in flags and "--restricted" in flags


@pytest.mark.parametrize("caller_mode", [None, "worker", "other"])
@pytest.mark.parametrize("ask", [{"mode": "representative"}, {"mode": "secretary"},
                                 {"mode": "delegate"}, {"name": "representative"},
                                 {"name": "secretary"}])
def test_a_session_may_not_start_a_reserved_name_or_mode(home, launched, caller_mode, ask):
    parent_session(home, mode=caller_mode)
    out = start({**BASE, "_caller_pid": os.getpid(), **ask})
    assert out["ok"] is False and "front door" in out["reason"]
    assert launched == []


def test_a_worker_or_unmoded_caller_may_start_any_other_mode(home, launched):
    parent_session(home, mode="worker")
    assert start({**BASE, "mode": "rep2", "_caller_pid": os.getpid()})["ok"]
    from awm.cx import config

    for f in os.listdir(config.starts_dir()):
        os.unlink(config.starts_dir() / f)
    home.set_workers({})
    parent_session(home)  # no lineage: an ordinary session
    assert start({**BASE, "name": "other", "mode": "rep3",
                  "_caller_pid": os.getpid()})["ok"]


@pytest.mark.parametrize("field", ["tools", "allowed_tools", "disallowed_tools"])
@pytest.mark.parametrize("bad", ["Bash;rm", "-x", "Read,Write(", "a b", ["ok", "--evil"],
                                 "Bash(a,b)", 5])
def test_tool_lists_are_validated(home, launched, field, bad):
    out = start({**BASE, field: bad})
    assert out["ok"] is False and field in out["reason"]


def test_tool_rules_with_a_specifier_are_accepted(home, launched):
    out = start({**BASE, "allowed_tools": ["Read(./**)", "mcp__awm__*", "Bash(git *)"]})
    assert out["ok"]


def test_a_caller_whose_mode_is_unknown_may_not_start(home, launched):
    parent_session(home)
    home.roster.write_text("{ not json")
    out = start({**BASE, "_caller_pid": os.getpid()})
    assert out["ok"] is False and "determined" in out["reason"]
    assert launched == []


def test_an_invalid_caller_pid_is_unknown_not_unrestricted(home, launched):
    for bad in (0, -1, "12", True, 1.5):
        out = start({**BASE, "_caller_pid": bad})
        assert out["ok"] is False, bad
    assert launched == []


# --- the reconcile prune -----------------------------------------------------


def _age(job):
    from awm.cx import config

    path = config.starts_dir() / f"{job}.json"
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def test_prune_drops_lineage_only_after_the_job_is_removed(home, launched):
    from awm.cx import lifecycle

    start(BASE)
    path = _age(CHILD_JOB)
    home.set_workers({})
    assert lifecycle.prune_lineage() == [], "stopped, but its conversation remains"
    assert path.exists()
    os.unlink(home.jobs / CHILD_JOB / "state.json")
    assert lifecycle.prune_lineage() == [CHILD_JOB]
    assert not path.exists()


def test_a_stopped_and_reattached_session_keeps_its_mode(home, launched):
    from awm.cx import lifecycle

    start({**BASE, "mode": "representative"})
    _age(CHILD_JOB)
    home.set_workers({})
    assert lifecycle.prune_lineage() == []
    home.add(CHILD_JOB, "demo", os.getppid())  # `claude attach` brings it back
    assert lifecycle.mode_of(os.getppid()) == "representative"


def test_prune_keeps_lineage_of_a_held_job(home, launched):
    from awm.cx import lifecycle

    start(BASE)
    path = _age(CHILD_JOB)
    os.unlink(home.jobs / CHILD_JOB / "state.json")
    assert lifecycle.prune_lineage() == [] and path.exists()


def test_prune_leaves_a_fresh_record_alone(home, launched):
    from awm.cx import config, lifecycle

    start(BASE)
    home.set_workers({})
    os.unlink(home.jobs / CHILD_JOB / "state.json")
    assert lifecycle.prune_lineage() == []
    assert (config.starts_dir() / f"{CHILD_JOB}.json").exists()


def test_prune_does_nothing_when_the_roster_is_unreadable(home, launched):
    from awm.cx import lifecycle

    start(BASE)
    path = _age(CHILD_JOB)
    os.unlink(home.jobs / CHILD_JOB / "state.json")
    home.roster.write_text("{ not json")
    assert lifecycle.prune_lineage() == [] and path.exists()


def test_prune_does_nothing_when_the_roster_has_no_workers_table(home, launched):
    from awm.cx import lifecycle

    start(BASE)
    path = _age(CHILD_JOB)
    os.unlink(home.jobs / CHILD_JOB / "state.json")
    home.roster.write_text(json.dumps({"supervisorPid": os.getpid()}))
    assert lifecycle.prune_lineage() == [] and path.exists()


def test_prune_does_nothing_when_the_jobs_directory_is_missing(home, launched):
    import shutil

    from awm.cx import lifecycle

    start(BASE)
    path = _age(CHILD_JOB)
    home.set_workers({})
    shutil.rmtree(home.jobs)
    assert lifecycle.prune_lineage() == [] and path.exists()


async def test_the_reconcile_tick_prunes_and_adopts(home, launched, monkeypatch):
    from awm.cx import config, lifecycle, reconcile, remove, seed

    assert (await lifecycle.start(BASE))["ok"]
    path = _age(CHILD_JOB)
    home.set_workers({})
    os.unlink(home.jobs / CHILD_JOB / "state.json")
    monkeypatch.setenv("AWM_CX_WANT", "0")

    async def nothing(now=None):
        return []

    adopted = []
    monkeypatch.setattr(lifecycle, "adopt_pending", lambda: adopted.append(1) or [])
    monkeypatch.setattr(remove, "apply", nothing)
    monkeypatch.setattr(seed, "seed_one", lambda: None)
    await reconcile.Loop().tick()
    assert not path.exists() and adopted == [1]


# --- mode_of: three answers --------------------------------------------------


def test_mode_of_reads_the_mode_through_the_roster(home, launched):
    from awm.cx import lifecycle

    start({**BASE, "mode": "representative"})
    assert lifecycle.mode_of(os.getppid()) == "representative"


def test_mode_of_is_none_only_for_a_pid_that_is_positively_not_cx_started(home, launched):
    from awm.cx import lifecycle

    start(BASE)
    home.add("dddddddd", "<warm otter>", os.getpid())
    assert lifecycle.mode_of(os.getpid()) is None, "a pool session has no lineage"
    home.record(1, "interactive")
    assert lifecycle.mode_of(1) is None, "an interactive terminal"


def test_mode_of_is_none_for_a_pid_with_no_record_at_all(home, launched):
    from awm.cx import lifecycle

    lifecycle.ensure_starts_dir()
    assert lifecycle.mode_of(os.getpid()) is None


@pytest.mark.parametrize("bad", [None, 0, -3, True, "12", 1.5])
def test_mode_of_an_invalid_pid_is_unknown(home, bad):
    from awm.cx import lifecycle

    assert lifecycle.mode_of(bad) == "unknown"


def test_mode_of_is_unknown_when_the_roster_is_unreadable(home, launched, monkeypatch):
    from awm.claudedaemon import sessionmode
    from awm.cx import lifecycle

    monkeypatch.setattr(sessionmode, "_may_be_a_session", lambda pid: True)
    start({**BASE, "mode": "representative"})
    home.roster.write_text("{ not json")
    assert lifecycle.mode_of(os.getppid()) == "unknown"


def test_mode_of_keeps_the_recorded_mode_when_the_roster_is_missing(home, launched):
    from awm.cx import lifecycle

    start({**BASE, "mode": "rep2"})
    home.record(os.getppid(), "bg", CHILD_JOB)
    home.roster.unlink()
    assert lifecycle.mode_of(os.getppid()) == "rep2"


def test_mode_of_is_unknown_for_a_bg_session_when_the_roster_is_missing_and_no_lineage(
        home, launched):
    from awm.cx import config, lifecycle

    start(BASE)
    home.record(os.getppid(), "bg", CHILD_JOB)
    home.roster.unlink()
    os.unlink(config.starts_dir() / f"{CHILD_JOB}.json")
    assert lifecycle.mode_of(os.getppid()) == "unknown"


def test_mode_of_is_unknown_when_a_cx_launched_job_lost_its_lineage(home, launched):
    import shutil

    from awm.cx import config, lifecycle

    start(BASE)
    workers = home.workers()
    workers[CHILD_JOB]["dispatch"]["launch"] = {"args": ["--permission-mode=dontAsk"]}
    home.set_workers(workers)
    shutil.rmtree(config.starts_dir())
    assert lifecycle.mode_of(os.getppid()) == "unknown"


def test_mode_of_is_none_for_a_job_without_cx_launch_flags_and_no_lineage(home, launched):
    import shutil

    from awm.cx import config, lifecycle

    start(BASE)
    shutil.rmtree(config.starts_dir())
    assert lifecycle.mode_of(os.getppid()) is None


@pytest.mark.parametrize("what", ["jobs", "sessions"])
def test_mode_of_survives_a_missing_jobs_or_sessions_directory(home, launched, what):
    import shutil

    from awm.cx import lifecycle

    start({**BASE, "mode": "rep2"})
    shutil.rmtree({"jobs": home.jobs, "sessions": home.sessions}[what])
    assert lifecycle.mode_of(os.getppid()) == "rep2"


def test_a_corrupt_record_of_another_job_does_not_change_the_answer(home, launched):
    from awm.cx import config, lifecycle

    start({**BASE, "mode": "rep2"})
    (config.starts_dir() / "cccccccc.json").write_text("{ not json")
    (config.starts_dir() / "pending-0123456789abcdef.json").write_text("{ not json")
    assert lifecycle.mode_of(os.getppid()) == "rep2"


def test_mode_of_is_unknown_when_the_callers_own_lineage_record_is_corrupt(home, launched):
    from awm.cx import config, lifecycle

    start(BASE)
    (config.starts_dir() / f"{CHILD_JOB}.json").write_text("{ not json")
    assert lifecycle.mode_of(os.getppid()) == "unknown"


def test_mode_of_is_unknown_when_the_recorded_mode_is_not_valid(home, launched):
    from awm.cx import config, lifecycle

    start(BASE)
    lineage(CHILD_JOB, mode="has space")
    assert lifecycle.mode_of(os.getppid()) == "unknown"


def test_mode_of_is_unknown_for_a_background_session_with_no_record(home):
    from awm.cx import lifecycle

    lifecycle.ensure_starts_dir()
    home.record(os.getpid(), "bg", "eeeeeeee")  # not in the roster, no lineage
    assert lifecycle.mode_of(os.getpid()) == "unknown"


def test_mode_of_uses_the_lineage_of_a_bg_session_missing_from_the_roster(home):
    from awm.cx import lifecycle

    lineage("eeeeeeee", mode="representative")
    home.record(os.getpid(), "bg", "eeeeeeee")
    assert lifecycle.mode_of(os.getpid()) == "representative"


def test_mode_of_is_unknown_for_a_stale_session_record(home):
    from awm.cx import lifecycle

    lifecycle.ensure_starts_dir()
    home.record(os.getpid(), "interactive")
    path = home.sessions / f"{os.getpid()}.json"
    rec = json.loads(path.read_text())
    rec["procStart"] = "1"
    path.write_text(json.dumps(rec))
    assert lifecycle.mode_of(os.getpid()) == "unknown"


def test_mode_of_docstring_states_the_contract():
    from awm.cx import lifecycle

    doc = lifecycle.mode_of.__doc__
    assert "positively not" in doc and '"unknown"' in doc


# --- the parent comes from a verified session record -------------------------


def test_parent_is_none_without_a_verified_record(home, launched):
    # a roster match alone no longer names a parent
    home.add(PARENT_JOB, "caller", os.getpid())
    assert start({**BASE, "_caller_pid": os.getpid()})["parent"] is None


def test_parent_of_an_interactive_caller_is_its_session_id(home, launched):
    home.record(os.getpid(), "interactive")
    assert start({**BASE, "_caller_pid": os.getpid()})["parent"] == f"sid-{os.getpid()}"


def test_a_stale_record_names_no_parent(home, launched):
    from awm.cx import lifecycle

    home.record(os.getpid(), "bg", PARENT_JOB)
    path = home.sessions / f"{os.getpid()}.json"
    rec = json.loads(path.read_text())
    rec["procStart"] = "1"
    path.write_text(json.dumps(rec))
    assert lifecycle._parent_of(os.getpid()) is None


# --- list --------------------------------------------------------------------


def test_list_merges_roster_lineage_and_interactive_sessions(home, launched, monkeypatch):
    from awm.cx import lifecycle

    home.add(PARENT_JOB, "<warm saiga>", os.getpid(), started_ms=1_600_000_000_000)
    start({**BASE, "mode": "rep"})
    me = os.getpid()
    home.record(me, "interactive", name="dev")
    rec = json.loads((home.sessions / f"{me}.json").read_text())
    rec["cwd"] = str(home.projects / "awm" / "demo")
    (home.sessions / f"{me}.json").write_text(json.dumps(rec))
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


def test_list_carries_needs_and_detail_from_the_job_record(home, monkeypatch):
    from awm.cx import lifecycle

    monkeypatch.setattr(lifecycle, "_tmux_panes", dict)
    home.add(PARENT_JOB, "<warm saiga>", os.getpid())
    path = home.jobs / PARENT_JOB / "state.json"
    rec = json.loads(path.read_text())
    rec.update(state="blocked", needs="login required \u2014 run /login", detail="d" * 500)
    path.write_text(json.dumps(rec))
    row = next(r for r in lifecycle.collect() if r["job"] == PARENT_JOB)
    assert row["state"] == "blocked" and row["needs"] == "login required \u2014 run /login"
    assert row["detail"] == "d" * 200


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


async def test_the_list_verb_applies_the_peer_refusal(home, launched, peers):
    from awm.cx import hub_adapter

    for who in ("peer", "peer:john", "peer:stranger"):
        out = await hub_adapter.HANDLERS["list"]({}, who)
        assert out["ok"] is False and "sessions" not in out, who
    out = await hub_adapter.HANDLERS["list"]({}, "peer:capella")
    assert "sessions" in out


def test_the_old_launcher_is_gone():
    from awm.cx import lifecycle

    for name in ("launch_argv", "_user_manager_env", "launch_session"):
        assert not hasattr(lifecycle, name), name


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
