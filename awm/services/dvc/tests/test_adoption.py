"""The adopt sweep — the one rule that covers every way a run loses its watcher.

A service restart mid-transfer, a killed console script, a manual verb call that
nobody is watching, a row orphaned between its insert and its submit: all of
them look identical in the table, and all of them are fixed by attaching a
watcher to any live row that has none. The load-bearing assertion is that
adoption never *resubmits* — the Globus task id is authoritative, and a fresh
submission id would genuinely duplicate a running multi-hour transfer.
"""

from __future__ import annotations

import asyncio
from time import time

import pytest

from awm.dvc import scheduler as schedmod


class FakeAdapter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, topic, payload):
        self.events.append((topic, payload))


@pytest.fixture()
def adopter(dvc_db, monkeypatch):
    """A scheduler that cannot submit, cannot sleep, and records what it emits."""
    monkeypatch.setattr(schedmod, "WATCH_POLL_S", 0)
    monkeypatch.setattr(schedmod, "load_config", lambda: object())

    def never(*a, **k):
        raise AssertionError("adoption must never submit a new transfer")

    monkeypatch.setattr(schedmod.globus, "submit", never)
    monkeypatch.setattr(
        schedmod.globus, "task_status",
        lambda cfg, task_id: {
            "status": "SUCCEEDED", "done": True, "files": 1,
            "files_transferred": 1, "files_skipped": 0,
            "bytes_transferred": 8, "faults": 0, "nice_status": "",
        },
    )
    adapter = FakeAdapter()
    return schedmod.Scheduler(adapter=adapter, dao=dvc_db, jitter_s=0), adapter


async def test_a_live_run_left_by_a_dead_process_is_adopted_to_a_verdict(
    adopter, dvc_db
):
    s, _ = adopter
    # What a service restart leaves behind: a row mid-transfer, no watcher.
    run = dvc_db.begin_run("cache_sync", trigger="schedule")
    dvc_db.mark_submitted(run["id"], "task-abc")

    await s._adopt_sweep()
    await asyncio.gather(*s._watching.values())

    assert dvc_db.get_run(run["id"])["status"] == "SUCCEEDED"


async def test_adoption_does_not_stack_a_second_watcher_on_one_run(adopter, dvc_db):
    s, _ = adopter
    run = dvc_db.begin_run("cache_sync")
    dvc_db.mark_submitted(run["id"], "task-abc")

    await s._adopt_sweep()
    first = dict(s._watching)
    await s._adopt_sweep()

    assert s._watching == first


async def test_a_finished_run_is_not_adopted(adopter, dvc_db):
    s, _ = adopter
    run = dvc_db.begin_run("cache_sync")
    dvc_db.mark_submitted(run["id"], "task-abc")
    dvc_db.finish(run["id"], "SUCCEEDED")

    await s._adopt_sweep()

    assert s._watching == {}


async def test_a_row_that_never_reached_globus_becomes_an_error_not_a_zombie(
    adopter, dvc_db
):
    """Inserted, then the process died before submit returned. Nothing to adopt."""
    s, adapter = adopter
    run = dvc_db.begin_run("cache_sync")
    dvc_db.execute(
        "UPDATE dvc_runs SET started_at = ? WHERE id = ?",
        (time() - schedmod.ORPHAN_AFTER_S - 1, run["id"]),
    )

    await s._adopt_sweep()
    row = dvc_db.get_run(run["id"])

    assert row["status"] == "error"
    assert "no Globus task" in row["error"]
    assert [p["status"] for _, p in adapter.events] == ["error"]
    # ...and the job's slot is free again, so tonight's run can proceed.
    assert dvc_db.begin_run("cache_sync") is not None


async def test_a_run_still_submitting_is_given_time_before_being_declared_orphaned(
    adopter, dvc_db
):
    """A whole-workspace pin scan takes seconds; that is not an orphan."""
    s, _ = adopter
    run = dvc_db.begin_run("workspace_backup")

    await s._adopt_sweep()

    assert dvc_db.get_run(run["id"])["status"] == "submitting"
    assert s._watching == {}
