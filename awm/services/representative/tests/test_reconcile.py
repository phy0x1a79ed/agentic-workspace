"""The reconcile loop: one representative and one secretary, started when missing."""

from __future__ import annotations

import asyncio

import pytest

from awm.representative import reconcile

from stubs import FakeCx, personas, session_row

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def loop_for(cx, queue, **kw):
    return reconcile.Loop(queue, cx=cx, personas=kw.pop("personas", personas()), **kw)


def ours(queue, cx, role, job="abc12345", **row):
    """A live session of the role that the door itself started."""
    queue.record_session(role, job)
    cx.rows.append(session_row(role, job=job, **row))


async def test_a_missing_representative_and_secretary_are_started_from_their_launch_configs(queue):
    cx = FakeCx()
    await loop_for(cx, queue).tick()
    assert [s["mode"] for s in cx.started] == ["representative", "secretary"]
    spec = cx.started[0]
    assert spec["permission"] == "dontAsk"
    assert spec["disallowed_tools"] == ["Bash", "Edit", "Write"]
    assert spec["remote_control"] is True


async def test_a_second_tick_does_not_start_a_second_one(queue):
    cx = FakeCx()
    loop = loop_for(cx, queue)
    await loop.tick()
    await loop.tick()
    await loop.tick()
    assert len(cx.started) == 2
    assert set(loop.jobs) == {"representative", "secretary"}


async def test_the_job_ids_it_starts_are_recorded_and_survive_a_restart(queue, tmp_path):
    import os

    from awm.representative.store import Queue

    cx = FakeCx()
    first = loop_for(cx, queue)
    await first.tick()
    assert queue.session_jobs("representative") == {cx.rows[0]["job"]}
    os.close(first._lock_fd)  # the old process died, and the kernel dropped its lock
    second = loop_for(cx, Queue(tmp_path / "door.db"))
    await second.tick()
    assert second.status()["holds_lock"] is True
    assert len(cx.started) == 2  # the restarted door recognises its own sessions


async def test_a_live_representative_is_left_alone_and_only_the_secretary_started(queue):
    cx = FakeCx()
    ours(queue, cx, "representative")
    await loop_for(cx, queue).tick()
    assert [s["mode"] for s in cx.started] == ["secretary"]


async def test_a_renamed_representative_still_counts(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", name="my pet name")
    ours(queue, cx, "secretary", job="def67890")
    await loop_for(cx, queue).tick()
    assert cx.started == []


async def test_a_session_with_the_mode_label_that_the_door_did_not_start_is_not_trusted(queue, caplog):
    cx = FakeCx([session_row("representative", job="evil0001"),
                 session_row("secretary", job="evil0002", name="other")])
    with caplog.at_level("INFO"):
        await loop_for(cx, queue).tick()
    assert [s["mode"] for s in cx.started] == ["representative", "secretary"]
    assert "did not start" in caplog.text


async def test_a_stranger_does_not_make_the_door_forget_its_own(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", job="mine0001")
    cx.rows.append(session_row("representative", job="evil0001"))
    loop = loop_for(cx, queue)
    await loop.tick()
    assert [s["mode"] for s in cx.started] == ["secretary"]
    assert loop.jobs["representative"] == "mine0001"
    assert await loop.representative_job() == "mine0001"


async def test_a_dead_representative_is_replaced_within_one_tick(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", job="old00001", state="gone")
    ours(queue, cx, "secretary", job="sec00001")
    loop = loop_for(cx, queue)
    await loop.tick()
    assert [s["mode"] for s in cx.started] == ["representative"]
    await loop.tick()
    assert len(cx.started) == 1
    assert queue.session_jobs("representative") >= {"old00001", cx.rows[-1]["job"]}


async def test_two_live_copies_are_left_alone(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", job="a1a1a1a1")
    ours(queue, cx, "representative", job="b2b2b2b2")
    ours(queue, cx, "secretary", job="c3c3c3c3")
    await loop_for(cx, queue).tick()
    assert cx.started == []
    assert not hasattr(cx, "stop")  # the door has no way to stop a session


async def test_nothing_starts_when_cx_cannot_say_what_is_running(queue):
    cx = FakeCx()
    cx.unavailable = True
    await loop_for(cx, queue).tick()
    assert cx.started == []


async def test_a_session_holding_the_name_that_the_door_did_not_start_is_not_taken_over(queue):
    cx = FakeCx([session_row("worker", name="front-door")])
    ours(queue, cx, "secretary", job="sec00001")
    await loop_for(cx, queue).tick()
    assert cx.started == []


async def test_a_launch_config_without_permission_is_never_started(queue):
    bad = personas()
    del bad.REPRESENTATIVE["permission"]
    cx = FakeCx()
    ours(queue, cx, "secretary")
    await loop_for(cx, queue, personas=bad).tick()
    assert cx.started == []


async def test_a_launch_config_without_a_mode_is_never_started(queue):
    bad = personas()
    bad.SECRETARY["mode"] = ""
    cx = FakeCx()
    ours(queue, cx, "representative")
    await loop_for(cx, queue, personas=bad).tick()
    assert cx.started == []


async def test_missing_personas_start_nothing(monkeypatch, queue):
    def no_module(name):
        raise ImportError("no personas yet")

    monkeypatch.setattr(reconcile.importlib, "import_module", no_module)
    cx = FakeCx()
    await reconcile.Loop(queue, cx=cx).tick()
    assert cx.started == []


async def test_a_refused_start_backs_off_before_trying_again(queue):
    cx = FakeCx()
    ours(queue, cx, "secretary")
    cx.refuse = "no claude code daemon is running"
    loop = loop_for(cx, queue, interval_s=0.01)
    await loop.tick()
    await loop.tick()
    assert len(cx.started) == 1  # the second tick is inside the retry window
    cx.refuse = None
    loop._next_try.clear()
    await loop.tick()
    assert len(cx.started) == 2
    assert "representative" in loop.jobs


async def test_a_second_door_process_does_not_reconcile(queue):
    cx_a, cx_b = FakeCx(), FakeCx()
    first, second = loop_for(cx_a, queue), loop_for(cx_b, queue)
    await first.tick()
    await second.tick()
    assert len(cx_a.started) == 2
    assert cx_b.started == []
    assert second.status()["holds_lock"] is False


async def test_the_lock_is_held_for_the_life_of_the_loop(queue):
    loop = loop_for(FakeCx(), queue)
    await loop.tick()
    assert loop.status()["holds_lock"] is True
    await loop.tick()
    assert loop.status()["holds_lock"] is True


async def test_a_started_representative_calls_the_hook_so_it_hears_of_waiting_cards(queue):
    started = []
    cx = FakeCx()
    ours(queue, cx, "secretary")
    await loop_for(cx, queue, on_started=started.append).tick()
    assert started == ["representative"]


async def test_run_outlives_a_tick_that_raises(monkeypatch, queue):
    loop = loop_for(FakeCx(), queue, interval_s=0.01)
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(loop, "tick", tick)
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.1)
    assert not task.done()
    assert len(calls) >= 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_representative_job_reads_cx_fresh(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", job="r1r1r1r1")
    loop = loop_for(cx, queue)
    assert await loop.representative_job() == "r1r1r1r1"
    assert (await loop.alive())["representative"] is True
    cx.rows.clear()
    assert await loop.representative_job() is None
    cx.unavailable = True
    assert await loop.representative_job() is None
    assert await loop.alive() == {"representative": None, "secretary": None}
