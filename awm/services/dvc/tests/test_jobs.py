"""``run_job`` — the one path every submission takes.

The single-flight guard used to be a ``threading.Lock`` in :mod:`awm.dvc.sync`,
which could only ever guard one of the three processes that submit. These tests
are about the replacement: the insert *is* the acquire, so two callers in
different processes get the same answer without a check-then-act window.
"""

from __future__ import annotations

import pytest

from awm.dvc import jobs, runs


@pytest.fixture()
def submits(dvc_db, monkeypatch):
    """Both jobs replaced by a recording stub. Nothing touches Globus."""
    calls: list[tuple[str, dict]] = []

    def stub(name):
        def submit(*, dry_run=False):
            calls.append((name, {"dry_run": dry_run}))
            if dry_run:
                return {"dry_run": True, "transfer_items": [{}]}
            # A fresh id per call, as Globus mints one per submission — and as
            # the one-row-per-task index requires.
            return {"submitted": True, "task_id": f"task-{len(calls)}"}
        return submit

    for name, spec in list(jobs.JOBS.items()):
        monkeypatch.setitem(
            jobs.JOBS, name, type(spec)(**{**spec.__dict__, "submit": stub(name)})
        )
    return calls


def test_a_submission_is_recorded_as_a_run(submits, dvc_db):
    result = jobs.run_job(jobs.CACHE_SYNC, trigger="schedule")

    row = dvc_db.get_run(result["run_id"])
    assert result["task_id"] == "task-1"
    assert (row["status"], row["task_id"], row["trigger"]) == (
        "running", "task-1", "schedule",
    )
    assert row["label"] == jobs.JOBS[jobs.CACHE_SYNC].label


def test_a_second_caller_is_declined_with_the_task_it_would_have_duplicated(
    submits, dvc_db
):
    jobs.run_job(jobs.CACHE_SYNC)

    second = jobs.run_job(jobs.CACHE_SYNC)

    assert second["submitted"] is False
    assert second["in_flight"] == "task-1"
    assert "force=true" in second["note"]
    assert len(submits) == 1  # the whole point: no second scan was started


def test_a_declined_run_still_leaves_a_row(submits, dvc_db):
    """Acceptance criterion 7: declined is an outcome, not silence."""
    jobs.run_job(jobs.CACHE_SYNC)
    jobs.run_job(jobs.CACHE_SYNC, trigger="schedule")

    skipped = dvc_db.history(job=jobs.CACHE_SYNC, status="skipped")

    assert len(skipped) == 1
    assert "still running" in skipped[0]["note"]


def test_force_supersedes_the_live_row_and_submits(submits, dvc_db):
    first = jobs.run_job(jobs.CACHE_SYNC)

    second = jobs.run_job(jobs.CACHE_SYNC, force=True)

    assert second["submitted"] is True
    assert dvc_db.get_run(first["run_id"])["status"] == "skipped"
    assert len(submits) == 2


def test_the_two_jobs_do_not_block_each_other(submits):
    assert jobs.run_job(jobs.CACHE_SYNC)["submitted"] is True
    assert jobs.run_job(jobs.WORKSPACE_BACKUP)["submitted"] is True


def test_a_dry_run_records_nothing_and_holds_no_slot(submits, dvc_db):
    result = jobs.run_job(jobs.WORKSPACE_BACKUP, dry_run=True)

    assert result["dry_run"] is True
    assert dvc_db.history(job=jobs.WORKSPACE_BACKUP) == []
    assert dvc_db.begin_run(jobs.WORKSPACE_BACKUP) is not None


def test_a_submit_that_raises_leaves_an_error_row_and_frees_the_slot(dvc_db,
                                                                    monkeypatch):
    spec = jobs.JOBS[jobs.CACHE_SYNC]

    def boom(*, dry_run=False):
        raise RuntimeError("globus: not logged in")

    monkeypatch.setitem(
        jobs.JOBS, jobs.CACHE_SYNC, type(spec)(**{**spec.__dict__, "submit": boom})
    )

    with pytest.raises(RuntimeError):
        jobs.run_job(jobs.CACHE_SYNC)

    row = dvc_db.history(job=jobs.CACHE_SYNC)[0]
    assert row["status"] == "error"
    assert "not logged in" in row["error"]
    assert dvc_db.begin_run(jobs.CACHE_SYNC) is not None


def test_an_unknown_job_is_rejected_by_name(submits):
    with pytest.raises(ValueError, match="unknown job"):
        jobs.run_job("nope")


def test_record_status_leaves_a_running_task_live_for_the_adopt_sweep(dvc_db):
    """A timed-out console script hands its transfer over rather than dropping it."""
    run = dvc_db.begin_run(jobs.CACHE_SYNC)
    dvc_db.mark_submitted(run["id"], "task-7")

    status = jobs.record_status(dvc_db, run["id"], {
        "status": "ACTIVE", "done": False, "files_transferred": 4,
    })

    assert status == "running"
    assert dvc_db.get_run(run["id"])["status"] == "running"
    assert dvc_db.live_run(jobs.CACHE_SYNC)["id"] == run["id"]


def test_seeding_creates_a_row_for_every_job_we_know_about(dvc_db):
    jobs.ensure_seeded(dvc_db)

    assert {r["name"] for r in dvc_db.list_jobs()} == set(jobs.JOBS)
    assert all(r["cron"] for r in dvc_db.list_jobs())


def test_a_sync_in_flight_at_cutover_is_migrated_into_the_run_table(dvc_db):
    """Deploying mid-transfer must not lose track of the running task."""
    from awm import config as awm_config

    state = awm_config.SERVICES_DIR / "dvc" / "last_sync.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"task_id": "old-task"}')

    jobs.ensure_seeded(dvc_db)

    row = dvc_db.live_run(jobs.CACHE_SYNC)
    assert row["task_id"] == "old-task"  # the adopt sweep will finish it
    assert row["trigger"] == "adopted"
    assert not state.exists()  # ...and it is never migrated twice


def test_the_legacy_state_path_follows_a_redirected_services_dir(dvc_db):
    """It renames a file, so reaching past a test's redirect is a real hazard."""
    from awm import config as awm_config

    assert jobs._legacy_state().is_relative_to(awm_config.SERVICES_DIR)


def test_the_jobs_and_the_run_status_domain_agree():
    """`skipped`/`error` are ours; the verdicts are Globus's, spelled its way."""
    from awm.dvc import globus

    assert set(runs.LIVE) == {"submitting", "running"}
    assert globus.TERMINAL == {"SUCCEEDED", "FAILED"}
