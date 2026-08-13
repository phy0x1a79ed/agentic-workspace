"""The watcher: a Globus task polled to a verdict, against a scripted sequence.

Globus has no push, so a verdict costs a poll — and every poll must reach the
run row *before* it reaches a subscriber, because ``adapter.emit`` is a silent
no-op while the control WS is down. These tests assert that ordering by checking
the row, not just the events.
"""

from __future__ import annotations

import pytest

from awm.dvc import scheduler as schedmod


class FakeAdapter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, topic, payload):
        self.events.append((topic, payload))


def doc(status, *, done=False, **kw):
    base = {
        "status": status, "done": done, "files": 10, "files_transferred": 0,
        "files_skipped": 0, "bytes_transferred": 0, "faults": 0, "nice_status": "",
    }
    base.update(kw)
    return base


@pytest.fixture()
def watched(dvc_db, monkeypatch):
    """A scheduler whose polls come from a scripted list and never sleep."""
    monkeypatch.setattr(schedmod, "WATCH_POLL_S", 0)
    monkeypatch.setattr(schedmod, "load_config", lambda: object())
    adapter = FakeAdapter()
    s = schedmod.Scheduler(adapter=adapter, dao=dvc_db, jitter_s=0)

    def script(docs):
        it = iter(docs)

        def fake_status(cfg, task_id):
            try:
                return next(it)
            except StopIteration:  # pragma: no cover - a test that under-scripts
                raise AssertionError("watcher polled past the end of the script")

        monkeypatch.setattr(schedmod.globus, "task_status", fake_status)

    return s, adapter, script


def _live_run(dao, job="cache_sync", task="task-1"):
    run = dao.begin_run(job, trigger="schedule")
    dao.mark_submitted(run["id"], task)
    return dao.get_run(run["id"])


def statuses(adapter):
    return [p["status"] for _, p in adapter.events]


async def test_a_task_that_succeeds_ends_the_run_with_its_counters(watched, dvc_db):
    s, adapter, script = watched
    run = _live_run(dvc_db)
    script([
        doc("ACTIVE", files_transferred=3),
        doc("SUCCEEDED", done=True, files_transferred=10, bytes_transferred=4096),
    ])

    await s._watch(run)
    row = dvc_db.get_run(run["id"])

    assert row["status"] == "SUCCEEDED"
    assert (row["files_transferred"], row["bytes_transferred"]) == (10, 4096)
    assert row["finished_at"] > 0
    assert statuses(adapter) == ["progress", "succeeded"]


async def test_a_task_that_fails_is_recorded_as_failed_not_as_silence(watched, dvc_db):
    s, adapter, script = watched
    run = _live_run(dvc_db)
    script([doc("FAILED", done=True, nice_status="PERMISSION_DENIED", faults=3)])

    await s._watch(run)
    row = dvc_db.get_run(run["id"])

    assert row["status"] == "FAILED"
    assert row["nice_status"] == "PERMISSION_DENIED"
    assert row["faults"] == 3
    assert statuses(adapter) == ["failed"]


async def test_the_verdict_uses_globus_wording_verbatim(watched, dvc_db):
    """So a run row and `dvc task` cannot mean different things by one word."""
    s, _, script = watched
    run = _live_run(dvc_db)
    script([doc("SUCCEEDED", done=True)])

    await s._watch(run)

    assert dvc_db.get_run(run["id"])["status"] in schedmod.globus.TERMINAL


async def test_unchanged_counters_do_not_produce_an_event_per_poll(watched, dvc_db):
    """Throttled by change: a quiet transfer is quiet, not a stream of duplicates."""
    s, adapter, script = watched
    run = _live_run(dvc_db)
    script([doc("ACTIVE"), doc("ACTIVE"), doc("ACTIVE"),
            doc("SUCCEEDED", done=True)])

    await s._watch(run)

    assert statuses(adapter) == ["progress", "succeeded"]


async def test_progress_is_emitted_again_when_the_counters_move(watched, dvc_db):
    s, adapter, script = watched
    run = _live_run(dvc_db)
    script([doc("ACTIVE", files_transferred=1), doc("ACTIVE", files_transferred=2),
            doc("SUCCEEDED", done=True)])

    await s._watch(run)

    assert statuses(adapter) == ["progress", "progress", "succeeded"]


async def test_the_row_is_updated_before_any_event_is_emitted(watched, dvc_db):
    """emit is lossy; the row is the record. Reversing this loses progress."""
    s, adapter, script = watched
    run = _live_run(dvc_db)
    seen: list[int] = []

    async def capture(topic, payload):
        seen.append(dvc_db.get_run(run["id"])["files_transferred"])

    adapter.emit = capture
    script([doc("ACTIVE", files_transferred=7), doc("SUCCEEDED", done=True,
                                                    files_transferred=10)])

    await s._watch(run)

    assert seen == [7, 10]


async def test_an_unqueryable_task_eventually_gets_an_error_row(watched, dvc_db,
                                                                monkeypatch):
    """It must not poll a dead task forever, and it must not stay 'running'."""
    s, adapter, _ = watched
    monkeypatch.setattr(schedmod, "_MAX_POLL_FAILURES", 3)

    def boom(cfg, task_id):
        raise schedmod.globus.GlobusError("no such task")

    monkeypatch.setattr(schedmod.globus, "task_status", boom)
    run = _live_run(dvc_db)

    await s._watch(run)
    row = dvc_db.get_run(run["id"])

    assert row["status"] == "error"
    assert "no such task" in row["error"]
    assert statuses(adapter) == ["error"]


async def test_a_long_run_is_alerted_once_and_keeps_polling(watched, dvc_db,
                                                            monkeypatch):
    """`stalled` is an alert, not a verdict — only Globus ends a run."""
    s, adapter, script = watched
    monkeypatch.setattr(schedmod, "STALL_AFTER_S", 0)
    run = _live_run(dvc_db)
    script([doc("ACTIVE", files_transferred=1), doc("ACTIVE", files_transferred=2),
            doc("SUCCEEDED", done=True)])

    await s._watch(run)
    row = dvc_db.get_run(run["id"])

    assert statuses(adapter).count("stalled") == 1
    assert row["status"] == "SUCCEEDED"
    assert row["alerted_at"] > 0
