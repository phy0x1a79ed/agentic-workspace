"""The launcher: its refusal, its command line, and its wait for a new job.

No test starts a real session. The process spawn is replaced by a fake that
writes the roster entry a real daemon would.
"""
from __future__ import annotations

import json
import os

import pytest

from awm.claudedaemon import launch


@pytest.fixture
def home(tmp_path):
    (tmp_path / "jobs").mkdir()
    return tmp_path


def roster_file(home, workers, supervisor=None):
    path = home / "roster.json"
    path.write_text(json.dumps({
        "supervisorPid": os.getpid() if supervisor is None else supervisor,
        "workers": workers}))
    return path


class FakeProc:
    async def wait(self):
        return 0

    def kill(self):
        pass


class Spawns(list):
    """Each fake spawn as (argv, kwargs); `hooks["on_spawn"]` runs on a spawn."""

    def __init__(self):
        super().__init__()
        self.hooks = {}


@pytest.fixture
def spawned(monkeypatch):
    calls = Spawns()

    async def fake_exec(*argv, **kw):
        calls.append((argv, kw))
        if "on_spawn" in calls.hooks:
            calls.hooks["on_spawn"]()
        return FakeProc()

    monkeypatch.setattr(launch.asyncio, "create_subprocess_exec", fake_exec)
    return calls


# --- the refusal ------------------------------------------------------------


def test_a_missing_daemon_is_a_refusal(home):
    assert "no claude code daemon" in launch.daemon_refusal(home / "none.json")
    dead = roster_file(home, {}, supervisor=2**22 + 12345)
    assert "no claude code daemon" in launch.daemon_refusal(dead)


def test_a_running_daemon_is_not_a_refusal(home):
    assert launch.daemon_refusal(roster_file(home, {})) is None


def test_a_daemon_in_another_mount_namespace_is_a_refusal_without_a_user_bus(
        home, monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)
    real = os.readlink
    monkeypatch.setattr(launch.os, "readlink", lambda p: (
        "mnt:[1]" if p == f"/proc/{os.getpid()}/ns/mnt" else real(p)))
    refusal = launch.daemon_refusal(roster_file(home, {}))
    assert "not reachable" in refusal and "mount namespace" in refusal


def test_an_unreadable_namespace_is_a_refusal_without_a_user_bus(home, monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)

    def denied(p):
        raise PermissionError(p)

    monkeypatch.setattr(launch.os, "readlink", denied)
    assert "not reachable" in launch.daemon_refusal(roster_file(home, {}))


def test_a_daemon_in_this_namespace_launches_directly_without_a_user_bus(
        home, monkeypatch):
    """A host with no systemd at all still launches."""
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)
    assert launch.daemon_refusal(roster_file(home, {})) is None


def test_a_user_bus_reaches_the_daemon_from_any_namespace(home, monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: {"XDG_RUNTIME_DIR": "/x"})
    monkeypatch.setattr(launch, "_shares_mount_namespace", lambda pid: False)
    assert launch.daemon_refusal(roster_file(home, {})) is None


async def test_an_unreachable_daemon_means_nothing_is_spawned(home, spawned, monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)
    monkeypatch.setattr(launch, "_shares_mount_namespace", lambda pid: False)
    with pytest.raises(launch.Refused, match="not reachable"):
        await launch.launch(cwd=home, name="n", roster_path=roster_file(home, {}),
                            jobs_dir=home / "jobs")
    with pytest.raises(launch.Refused, match="not reachable"):
        await launch.respawn("abc", roster_path=roster_file(home, {}),
                             jobs_dir=home / "jobs")
    assert spawned == []


async def test_no_daemon_means_nothing_is_spawned(home, spawned):
    with pytest.raises(launch.Refused, match="no claude code daemon"):
        await launch.launch(cwd=home, name="n", roster_path=home / "none.json",
                            jobs_dir=home / "jobs")
    assert spawned == []


# --- the command line -------------------------------------------------------


def test_the_transient_unit_never_kills_its_own_control_group(monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: {"XDG_RUNTIME_DIR": "/x"})
    argv, how, env = launch.build_argv(
        claude="/bin/claude", name="n", flags=["--effort", "low"], cwd="/w",
        env={"ANTHROPIC_MODEL": "m"}, prompt="do it", unit_prefix="awm-test")
    assert argv[:3] == ["systemd-run", "--user", "--quiet"]
    assert "--property=KillMode=process" in argv
    assert "--working-directory=/w" in argv
    assert "--setenv=ANTHROPIC_MODEL=m" in argv
    assert any(a.startswith("--unit=awm-test-") for a in argv)
    assert env == {"XDG_RUNTIME_DIR": "/x"}
    assert "user unit" in how
    # Everything before the first `--` belongs to systemd-run, everything after
    # to claude, and the prompt follows a second `--` or the CLI drops it.
    cmd = argv[argv.index("--") + 1:]
    assert cmd == ["/bin/claude", "--bg", "--name=n", "--effort", "low", "--", "do it"]


def test_no_prompt_means_no_separator(monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)
    argv, how, env = launch.build_argv(
        claude="/bin/claude", name="n", flags=[], cwd="/w", env={})
    assert argv == ["/bin/claude", "--bg", "--name=n"]
    assert env == {} and "directly" in how


def test_a_name_that_looks_like_a_flag_stays_one_argument(monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)
    argv, _, _ = launch.build_argv(
        claude="/bin/claude", name="--dangerously-skip-permissions", flags=[],
        cwd="/w", env={})
    assert argv == ["/bin/claude", "--bg", "--name=--dangerously-skip-permissions"]


# --- the wait ---------------------------------------------------------------


def worker(short, started):
    return {"replPid": os.getpid(), "startedAt": started}


async def test_the_new_job_is_the_one_that_appears_after_the_spawn(home, spawned):
    path = roster_file(home, {"old": worker("old", 1)})
    spawned.hooks["on_spawn"] = lambda: roster_file(
        home, {"old": worker("old", 1), "fresh": worker("fresh", 2)})
    s = await launch.launch(
        cwd=home, name="n", flags=["--x"], env={"K": "v"}, prompt="hi",
        claude="/bin/claude", roster_path=path, jobs_dir=home / "jobs",
        timeout=2)
    assert s.short == "fresh"
    argv, kw = spawned[0]
    assert kw["cwd"] == str(home)
    assert kw["env"]["K"] == "v"
    assert "/bin/claude" in argv and "hi" in argv


async def test_a_new_job_the_caller_rejects_does_not_count(home, spawned, monkeypatch):
    monkeypatch.setattr(launch, "POLL_S", 0.01)
    path = roster_file(home, {})
    spawned.hooks["on_spawn"] = lambda: roster_file(home, {"x": worker("x", 1)})
    with pytest.raises(TimeoutError, match="no new warm session"):
        await launch.launch(cwd=home, name="n", claude="/bin/claude",
                            roster_path=path, jobs_dir=home / "jobs",
                            accept=lambda s: False, label="warm", timeout=0.2)


async def test_no_new_job_is_a_timeout(home, spawned, monkeypatch):
    monkeypatch.setattr(launch, "POLL_S", 0.01)
    path = roster_file(home, {})
    with pytest.raises(TimeoutError, match="no new background session"):
        await launch.launch(cwd=home, name="n", claude="/bin/claude",
                            roster_path=path, jobs_dir=home / "jobs", timeout=0.1)


# --- respawn ----------------------------------------------------------------


def live(pid=None, proc_start=None):
    from awm.claudedaemon import roster

    pid = pid or os.getpid()
    return {"replPid": pid, "replProcStart": proc_start or roster.proc_start(pid),
            "startedAt": 1}


async def test_respawn_waits_for_the_same_job_to_come_back(home, spawned, monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)
    path = roster_file(home, {"other": live()})
    spawned.hooks["on_spawn"] = lambda: roster_file(
        home, {"other": live(), "seat1": live()})
    s = await launch.respawn("seat1", claude="/bin/claude", roster_path=path,
                             jobs_dir=home / "jobs", timeout=2)
    assert s.short == "seat1"
    assert spawned[0][0] == ("/bin/claude", "respawn", "seat1")


async def test_respawn_refuses_a_live_job(home, spawned):
    path = roster_file(home, {"seat1": live()})
    with pytest.raises(launch.Refused, match="running"):
        await launch.respawn("seat1", roster_path=path, jobs_dir=home / "jobs")
    assert spawned == []


async def test_respawn_waits_past_a_stale_roster_entry(home, spawned, monkeypatch):
    monkeypatch.setattr(launch, "POLL_S", 0.01)
    monkeypatch.setattr(launch, "user_manager_env", lambda: None)
    path = roster_file(home, {"seat1": live(proc_start="dead")})
    with pytest.raises(TimeoutError, match="seat1 did not come back"):
        await launch.respawn("seat1", claude="/bin/claude", roster_path=path,
                             jobs_dir=home / "jobs", timeout=0.1)
    assert len(spawned) == 1


async def test_respawn_needs_a_daemon(home, spawned):
    with pytest.raises(launch.Refused, match="no claude code daemon"):
        await launch.respawn("seat1", roster_path=home / "none.json",
                             jobs_dir=home / "jobs")
    assert spawned == []


def test_respawn_runs_in_a_unit_that_keeps_its_control_group(monkeypatch):
    monkeypatch.setattr(launch, "user_manager_env", lambda: {"XDG_RUNTIME_DIR": "/x"})
    argv, _, _ = launch.in_unit(["/bin/claude", "respawn", "j"], cwd="/h", env={},
                                unit_prefix="awm-cx-resume")
    assert "--property=KillMode=process" in argv
    assert argv[argv.index("--") + 1:] == ["/bin/claude", "respawn", "j"]
