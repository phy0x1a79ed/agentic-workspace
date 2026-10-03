"""The run table, against a real SQLite file.

A fake DAO would not exercise the partial unique index, and that index *is* the
single-flight guard — the thing standing between one nightly backup and two
concurrent full-cache scans. So these run against the real schema.
"""

from __future__ import annotations

import sqlite3
from time import time

import pytest

from awm.dvc import runs


def test_a_second_run_of_the_same_job_cannot_claim_the_slot(dvc_db):
    first = dvc_db.begin_run("cache_sync", trigger="schedule")

    assert first is not None
    assert dvc_db.begin_run("cache_sync", trigger="manual") is None


def test_the_guard_is_per_job_so_the_two_backups_run_concurrently(dvc_db):
    """Serializing them would skip the mirror every night the cache scan ran long."""
    assert dvc_db.begin_run("cache_sync") is not None
    assert dvc_db.begin_run("workspace_backup") is not None


def test_the_slot_frees_once_the_run_reaches_a_verdict(dvc_db):
    first = dvc_db.begin_run("cache_sync")
    dvc_db.mark_submitted(first["id"], "task-1")
    dvc_db.finish(first["id"], "SUCCEEDED", {"files": 5, "bytes_transferred": 99})

    second = dvc_db.begin_run("cache_sync")

    assert second is not None
    assert second["id"] != first["id"]


def test_a_skipped_row_does_not_hold_the_slot(dvc_db):
    dvc_db.record_skip("cache_sync", trigger="schedule", note="declined")

    assert dvc_db.begin_run("cache_sync") is not None


def test_force_retires_the_live_row_so_a_new_one_can_claim_the_slot(dvc_db):
    live = dvc_db.begin_run("cache_sync")
    dvc_db.mark_submitted(live["id"], "task-1")

    closed = dvc_db.close_live("cache_sync", note="superseded by a forced run")

    assert closed == 1
    assert dvc_db.begin_run("cache_sync") is not None
    assert dvc_db.get_run(live["id"])["status"] == "skipped"


def test_two_runs_cannot_record_the_same_globus_task(dvc_db):
    """Adoption idempotence: one task, one row, one verdict."""
    a = dvc_db.begin_run("cache_sync")
    dvc_db.mark_submitted(a["id"], "task-1")
    dvc_db.finish(a["id"], "SUCCEEDED")
    b = dvc_db.begin_run("cache_sync")

    with pytest.raises(sqlite3.IntegrityError):
        dvc_db.mark_submitted(b["id"], "task-1")


def test_a_poll_records_counters_without_ending_the_run(dvc_db):
    run = dvc_db.begin_run("workspace_backup")
    dvc_db.mark_submitted(run["id"], "task-9")

    dvc_db.record_poll(run["id"], {
        "files": 100, "files_transferred": 40, "files_skipped": 1,
        "bytes_transferred": 4096, "faults": 2, "nice_status": "PERMISSION_DENIED",
    })
    row = dvc_db.get_run(run["id"])

    assert row["status"] == "running"
    assert (row["files_transferred"], row["faults"]) == (40, 2)
    assert row["nice_status"] == "PERMISSION_DENIED"
    assert row["polled_at"] > 0 and row["finished_at"] == 0


def test_a_failed_submission_is_an_error_row_not_silence(dvc_db):
    run = dvc_db.begin_run("cache_sync")

    dvc_db.fail(run["id"], "GlobusError: not logged in")
    row = dvc_db.get_run(run["id"])

    assert row["status"] == "error"
    assert "not logged in" in row["error"]
    assert dvc_db.begin_run("cache_sync") is not None  # and the slot is free


def test_live_runs_finds_exactly_what_the_adopt_sweep_must_pick_up(dvc_db):
    submitting = dvc_db.begin_run("cache_sync")
    running = dvc_db.begin_run("workspace_backup")
    dvc_db.mark_submitted(running["id"], "task-2")
    dvc_db.record_skip("cache_sync", trigger="manual", note="x")

    ids = {r["id"] for r in dvc_db.live_runs()}

    assert ids == {submitting["id"], running["id"]}


def test_history_filters_by_job_status_and_time(dvc_db):
    old = dvc_db.begin_run("cache_sync")
    dvc_db.finish(old["id"], "FAILED")
    new = dvc_db.begin_run("cache_sync")
    dvc_db.finish(new["id"], "SUCCEEDED")
    dvc_db.record_skip("workspace_backup", trigger="manual", note="x")

    assert [r["id"] for r in dvc_db.history(job="cache_sync")] == [new["id"], old["id"]]
    assert [r["status"] for r in dvc_db.history(status="SUCCEEDED")] == ["SUCCEEDED"]
    assert dvc_db.history(since=time() + 60) == []
    assert len(dvc_db.history(limit=1)) == 1


def test_a_seeded_schedule_edit_survives_the_next_restart(dvc_db):
    """Defaults are code; the schedule is state. Re-seeding must not revert it."""
    dvc_db.seed_job("cache_sync", cron="0 4 * * *", catchup_window_s=72000)
    dvc_db.set_schedule("cache_sync", cron="@every 5m", enabled=False)

    dvc_db.seed_job("cache_sync", cron="0 4 * * *", catchup_window_s=72000)
    row = dvc_db.get_job("cache_sync")

    assert row["cron"] == "@every 5m"
    assert row["enabled"] == 0


def test_last_fire_is_persisted_so_catch_up_has_something_to_measure(dvc_db):
    dvc_db.seed_job("cache_sync", cron="0 4 * * *", catchup_window_s=72000)

    dvc_db.set_last_fire("cache_sync", 1_700_000_000.0)

    assert dvc_db.get_job("cache_sync")["last_fire_at"] == 1_700_000_000.0


def test_iso_and_to_epoch_round_trip():
    assert runs.iso(0) == ""
    assert runs.to_epoch("") is None
    assert runs.to_epoch("nonsense") is None
    assert runs.to_epoch(1_700_000_000) == 1_700_000_000.0
    assert runs.to_epoch(runs.iso(1_700_000_000)) == 1_700_000_000.0
