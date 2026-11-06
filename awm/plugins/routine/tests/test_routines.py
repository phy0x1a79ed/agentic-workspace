"""Routine definitions, the registry, runs, the failure chain and timers."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from awm.routine import definitions, timers
from awm.routine.definitions import DefinitionError
from awm.routine.runner import Runner
from awm.routine.store import LiveRunExists

from tests.conftest import git, write_routine

pytestmark = pytest.mark.smoke

BASIC = """
[routine]
name = "nightly"
description = "two steps"

[trigger]
timer = "*-*-* 03:00:00"

[[step]]
name = "one"
run = "echo one > out.txt"

[[step]]
name = "two"
run = "echo two >> out.txt"

[fixer]
peer = "alcor"
prompt = "Check the disk first."
"""


# -- definitions ---------------------------------------------------------

def test_parse_basic():
    r = definitions.parse(BASIC)
    assert r.name == "nightly"
    assert r.timer == "*-*-* 03:00:00" and r.call
    assert [s.name for s in r.steps] == ["one", "two"]
    assert r.fixer.peer == "alcor"
    assert definitions.Routine.from_dict(r.to_dict()) == r


@pytest.mark.parametrize("text, msg", [
    ("[routine]\nname='X'\n[[step]]\nrun='true'", "lowercase"),
    ("[routine]\nname='a'\nnode='mizar'\n[[step]]\nrun='true'", "names no node"),
    ("[routine]\nname='a'\n[trigger]\ncall=false\n[[step]]\nrun='true'", "needs a timer"),
    ("[routine]\nname='a'", "at least one"),
    ("[routine]\nname='a'\n[[step]]\nrun='true'\nkind='maybe'", "kind must be"),
    ("[routine]\nname='a'\n[[step]]\nrun='true'\nkind='condition'", "needs a prompt"),
    ("[routine]\nname='a'\n[[step]]\nname='s'\nrun='true'\n[[step]]\nname='s'\nrun='true'",
     "duplicate"),
    ("[routine]\nname='a'\n[[step]]\nrun='true'\n[fixer]\nhost='x'", "unknown keys"),
])
def test_parse_rejects(text, msg):
    with pytest.raises(DefinitionError, match=msg):
        definitions.parse(text)


# -- install --------------------------------------------------------------

def test_install_pins_commit(store, out, project_repo):
    p = write_routine(project_repo, "nightly", BASIC)
    runner = Runner(store, out)
    rec = runner.install("proj", str(p))
    assert rec["name"] == "nightly"
    assert rec["root"] == str(project_repo)
    assert rec["commit_sha"] == git(project_repo, "rev-parse", "HEAD")
    assert [r["name"] for r in store.list_routines("proj")] == ["nightly"]


def test_install_refuses_uncommitted(store, out, project_repo):
    p = write_routine(project_repo, "nightly", BASIC)
    p.write_text(BASIC.replace("two", "three"))
    with pytest.raises(ValueError, match="uncommitted"):
        Runner(store, out).install("proj", str(p))


# -- runs -----------------------------------------------------------------

async def test_trigger_runs_steps_in_order(store, out, project_repo):
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", BASIC)))
    run = await runner.trigger("proj", "nightly", caller="job abc", wait=True)
    assert run["state"] == "ok"
    assert run["caller"] == "job abc"
    assert [s["step"] for s in run["steps"]] == ["one", "two"]
    assert (project_repo / "out.txt").read_text() == "one\ntwo\n"
    assert out.starts == []


async def test_one_live_run(store, out, project_repo):
    body = BASIC.replace('run = "echo one > out.txt"', 'run = "sleep 5"')
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", body)))
    first = await runner.trigger("proj", "nightly")
    with pytest.raises(LiveRunExists):
        await runner.trigger("proj", "nightly")
    t0 = time.monotonic()
    res = await runner.cancel("proj", "nightly")
    assert time.monotonic() - t0 < 4
    assert res["cancelled"] and res["run"]["state"] == "cancelled"
    assert store.get_run(first["id"])["state"] == "cancelled"
    # The guard clears once the run ends.
    second = await runner.trigger("proj", "nightly")
    await runner.cancel("proj", "nightly")
    assert second["id"] != first["id"]


async def test_cancel_kills_children(store, out, project_repo, tmp_path):
    pidfile = tmp_path / "child.pid"
    body = BASIC.replace('run = "echo one > out.txt"',
                         f'run = "sleep 30 & echo $! > {pidfile}; wait"')
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", body)))
    await runner.trigger("proj", "nightly")
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    child = int(pidfile.read_text())
    await runner.cancel("proj", "nightly")
    await asyncio.sleep(0.2)
    assert not Path(f"/proc/{child}").exists() or "Z" in Path(
        f"/proc/{child}/stat").read_text().split()[2]


async def test_timer_only_refuses_call(store, out, project_repo):
    body = BASIC.replace('timer = "*-*-* 03:00:00"',
                         'timer = "*-*-* 03:00:00"\ncall = false')
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", body)))
    with pytest.raises(ValueError, match="timer only"):
        await runner.trigger("proj", "nightly")
    run = await runner.trigger("proj", "nightly", trigger="timer", wait=True)
    assert run["state"] == "ok"


# -- failure chain (R47, R48) ----------------------------------------------

FAILING = BASIC.replace('run = "echo two >> out.txt"', 'run = "echo boom; exit 3"')


async def test_failure_starts_fixer_on_declared_peer(store, out, project_repo):
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", FAILING)))
    run = await runner.trigger("proj", "nightly", caller="job abc", wait=True)
    assert run["state"] == "failed" and run["failed_step"] == "two"
    assert "boom" in run["error"]
    [start] = out.starts
    assert start["peer"] == "alcor"
    assert start["project"] == "proj" and start["scope"] == "fix-nightly"
    assert run["id"] in start["started_by"] and "job abc" in start["started_by"]
    assert "boom" in start["prompt"] and "Check the disk first." in start["prompt"]
    assert run["dispatched"] == [{"for": "two", "role": "fixer", "via": "agent",
                                  "job": "job1", "peer": "alcor", "errors": []}]
    assert out.dms == []


async def test_failure_falls_back_to_dm(store, out, project_repo):
    out.fail_start = True
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", FAILING)))
    run = await runner.trigger("proj", "nightly", wait=True)
    [d] = run["dispatched"]
    assert d["via"] == "dm" and "peer unreachable" in d["errors"][0]
    assert len(out.dms) == 1 and "failed at two" in out.dms[0]


async def test_failure_falls_back_to_log(store, out, project_repo, caplog):
    out.fail_start = out.fail_dm = True
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", FAILING)))
    run = await runner.trigger("proj", "nightly", wait=True)
    [d] = run["dispatched"]
    assert d["via"] == "log" and len(d["errors"]) == 2
    assert "no agent and no DM" in caplog.text


async def test_step_timeout_is_a_failure(store, out, project_repo):
    body = BASIC.replace('run = "echo one > out.txt"', 'run = "sleep 10"\ntimeout = 0.3')
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "nightly", body)))
    run = await runner.trigger("proj", "nightly", wait=True)
    assert run["state"] == "failed" and run["failed_step"] == "one"
    assert run["error"].startswith("timed out")
    assert run["steps"][0]["timed_out"] is True
    assert len(out.starts) == 1


# -- conditions (R34) -----------------------------------------------------

CONDITIONS = """
[routine]
name = "watch"

[[step]]
name = "holds"
kind = "condition"
run = "echo disk 95%; exit 0"
prompt = "Free some disk."

[[step]]
name = "quiet"
kind = "condition"
run = "exit 1"
prompt = "Never sent."

[agent]
peer = "alcor"
scope = "disk-care"
"""


async def test_condition_dispatches_agent(store, out, project_repo):
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(project_repo, "watch", CONDITIONS)))
    run = await runner.trigger("proj", "watch", wait=True)
    assert run["state"] == "ok"
    assert [s.get("held") for s in run["steps"]] == [True, False]
    [start] = out.starts
    assert start["peer"] == "alcor" and start["scope"] == "disk-care"
    assert "Free some disk." in start["prompt"] and "disk 95%" in start["prompt"]
    assert run["dispatched"][0]["role"] == "agent"


async def test_condition_error_code_is_failure(store, out, project_repo):
    runner = Runner(store, out)
    runner.install("proj", str(write_routine(
        project_repo, "watch", CONDITIONS.replace("exit 1", "exit 7"))))
    run = await runner.trigger("proj", "watch", wait=True)
    assert run["state"] == "failed" and run["failed_step"] == "quiet"
    assert [d["role"] for d in run["dispatched"]] == ["agent", "fixer"]


def test_orphaned_runs_close_on_restart(store):
    store.put_routine(project="p", name="r", root="/", path="/x", commit_sha="",
                      definition={"name": "r"})
    run = store.start_run(project="p", name="r", trigger="call")
    assert store.orphan_live() == 1
    assert store.get_run(run["id"])["state"] == "failed"
    store.start_run(project="p", name="r", trigger="call")


# -- timers (R38) ---------------------------------------------------------

def test_schedule_writes_units_and_enables(tmp_path):
    calls: list[list[str]] = []
    res = timers.schedule("awm", "full-tests", "*-*-* 03:00:00",
                          systemctl=calls.append, directory=tmp_path)
    unit = "awm-routine-awm-full-tests"
    assert res["unit"] == f"{unit}.timer"
    timer = (tmp_path / f"{unit}.timer").read_text()
    service = (tmp_path / f"{unit}.service").read_text()
    assert "OnCalendar=*-*-* 03:00:00" in timer and "Persistent=true" in timer
    assert "routine trigger --project awm --name full-tests --trigger timer" in service
    assert calls == [["daemon-reload"], ["enable", "--now", f"{unit}.timer"]]

    calls.clear()
    res = timers.unschedule("awm", "full-tests", systemctl=calls.append,
                            directory=tmp_path)
    assert res["removed"] is True
    assert not list(tmp_path.iterdir())
    assert calls == [["disable", "--now", f"{unit}.timer"], ["daemon-reload"]]


def test_schedule_rejects_bad_calendar(tmp_path):
    with pytest.raises(ValueError):
        timers.schedule("awm", "x", "not a calendar", systemctl=lambda a: None,
                        directory=tmp_path)
    assert not list(tmp_path.iterdir())


def test_unit_name_is_safe():
    assert timers.unit_name("a b", "c/d") == "awm-routine-a_b-c_d"
