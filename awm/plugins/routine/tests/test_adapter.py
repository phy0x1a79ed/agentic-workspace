"""The routine tool's surface: names, handlers and a scheduled routine's lifecycle."""

from __future__ import annotations

import pytest

from awm.routine import hub_adapter
from awm.routine.hub_adapter import ROUTINE_FUNCTIONS, RoutineService

from tests.conftest import write_routine

pytestmark = pytest.mark.smoke

ROUTINE = """
[routine]
name = "tick"
[trigger]
timer = "hourly"
[[step]]
run = "true"
"""


def test_every_function_is_routine_verb_with_a_handler(store, out):
    svc = RoutineService(store, out)
    names = {f["name"] for f in ROUTINE_FUNCTIONS}
    assert names == set(svc.handlers())
    for f in ROUTINE_FUNCTIONS:
        assert f["tool"] == f"routine_{f['name']}"


def test_caller_label():
    assert hub_adapter.caller_label({"_caller_pid": 42}, None) == "pid 42"
    assert hub_adapter.caller_label({}, "peer:altair") == "peer:altair"
    assert hub_adapter.caller_label({}, None) == ""


async def test_lifecycle_through_handlers(store, out, project_repo, tmp_path):
    calls: list[list[str]] = []
    svc = RoutineService(store, out, systemctl=calls.append, unit_dir=tmp_path / "units")
    path = write_routine(project_repo, "tick", ROUTINE)
    svc.install({"project": "proj", "path": str(path)})
    svc.schedule({"project": "proj", "name": "tick"})
    assert (tmp_path / "units" / "awm-routine-proj-tick.timer").exists()
    run = await svc.trigger({"project": "proj", "name": "tick", "wait": True,
                             "_caller_pid": 7})
    assert run["state"] == "ok" and run["caller"] == "pid 7"
    [listed] = svc.list({})["routines"]
    assert listed["scheduled"] and listed["last_run"]["state"] == "ok"
    assert svc.runs({"id": run["id"]})["id"] == run["id"]
    assert svc.uninstall({"project": "proj", "name": "tick"}) == {"removed": True}
    assert not (tmp_path / "units" / "awm-routine-proj-tick.timer").exists()
    assert ["disable", "--now", "awm-routine-proj-tick.timer"] in calls


async def test_bad_trigger_kind(store, out):
    with pytest.raises(ValueError):
        await RoutineService(store, out).trigger(
            {"project": "p", "name": "n", "trigger": "cron"})
