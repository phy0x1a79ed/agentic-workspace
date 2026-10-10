"""The reconcile loop: one representative and one secretary, started when missing."""

from __future__ import annotations

import asyncio
import time

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
    assert cx.stopped == []  # duplicates are never stopped


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


async def test_a_start_that_outlived_its_call_is_adopted_on_the_next_tick(queue):
    cx = FakeCx()
    cx.refuse = "cx start failed: timed out"
    cx.rows.append(session_row("representative", job="late0001", caller="local"))
    cx.rows[-1]["name"] = personas().REPRESENTATIVE["name"]
    ours(queue, cx, "secretary", job="sec00001")
    woke = []
    loop = loop_for(cx, queue, on_started=woke.append)
    await loop.tick()
    assert cx.started == []  # nothing new is started
    assert queue.session_jobs("representative") == {"late0001"}
    assert loop.jobs["representative"] == "late0001"
    assert woke == ["representative"]  # the new owner hears of waiting cards
    assert await loop.representative_job() == "late0001"


@pytest.mark.parametrize("row", [
    dict(caller=None),                          # hand-started: no lineage
    dict(caller="local", parent="par00001"),    # started by a session, not by the door
    dict(caller="local", name="other name"),    # not the reserved name
])
async def test_only_the_doors_own_unrecorded_session_is_adopted(queue, row):
    cx = FakeCx()
    ours(queue, cx, "secretary", job="sec00001")
    fields = dict(name=personas().REPRESENTATIVE["name"], caller="local", parent=None, **{})
    fields.update(row)
    cx.rows.append(session_row("representative", job="odd00001", **fields))
    cx.refuse = "no"
    await loop_for(cx, queue).tick()
    assert queue.session_jobs("representative") == set()


def work_personas(where=("awm", "door-work")):
    stub = personas()
    stub.work_where = lambda: where
    return stub


class ScopeCalls:
    """A stand-in for the gateway call that creates a scope's worktree."""

    def __init__(self, projects, *, fail=None, make=True):
        self.projects, self.fail, self.make = projects, fail, make
        self.calls = []

    async def __call__(self, service, fn, args, **kw):
        self.calls.append((service, fn, args))
        if self.fail:
            raise RuntimeError(self.fail)
        if self.make:
            (self.projects / args["project"] / args["scope"]).mkdir(parents=True)
        return {"ok": True}


async def test_the_work_scope_is_created_once_when_it_is_missing(queue, tmp_path):
    scopes = ScopeCalls(tmp_path / "projects")
    loop = loop_for(FakeCx(), queue, personas=work_personas(), scope_call=scopes)
    await loop.tick()
    await loop.tick()
    assert scopes.calls == [("scopes", "scope_create", {"project": "awm", "scope": "door-work"})]


async def test_an_existing_work_scope_is_left_alone(queue, tmp_path):
    (tmp_path / "projects" / "awm" / "door-work").mkdir(parents=True)
    scopes = ScopeCalls(tmp_path / "projects")
    await loop_for(FakeCx(), queue, personas=work_personas(), scope_call=scopes).tick()
    assert scopes.calls == []


async def test_the_work_scope_comes_from_personas(queue, tmp_path):
    scopes = ScopeCalls(tmp_path / "projects")
    await loop_for(FakeCx(), queue, personas=work_personas(("other", "nested/work")),
                   scope_call=scopes).tick()
    assert scopes.calls[0][2] == {"project": "other", "scope": "nested/work"}


async def test_a_failed_scope_create_is_retried_next_tick_and_roles_still_start(queue, tmp_path):
    scopes = ScopeCalls(tmp_path / "projects", fail="scopes is down")
    cx = FakeCx()
    loop = loop_for(cx, queue, personas=work_personas(), scope_call=scopes)
    await loop.tick()
    assert [s["mode"] for s in cx.started] == ["representative", "secretary"]
    scopes.fail = None
    await loop.tick()
    assert len(scopes.calls) == 2
    assert (tmp_path / "projects" / "awm" / "door-work").is_dir()


async def test_a_create_that_leaves_no_worktree_counts_as_a_failure(queue, tmp_path):
    scopes = ScopeCalls(tmp_path / "projects", make=False)
    loop = loop_for(FakeCx(), queue, personas=work_personas(), scope_call=scopes)
    await loop.tick()
    await loop.tick()
    assert len(scopes.calls) == 2


async def test_no_scope_is_created_while_the_door_is_off(queue, tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_FRONT_DOOR", "0")
    scopes = ScopeCalls(tmp_path / "projects")
    await loop_for(FakeCx(), queue, personas=work_personas(), scope_call=scopes).tick()
    assert scopes.calls == []


def test_the_real_personas_default_to_awm_door_work(monkeypatch):
    from awm.representative import personas as real

    monkeypatch.delenv("AWM_DOOR_WORK_PROJECT", raising=False)
    monkeypatch.delenv("AWM_DOOR_WORK_SCOPE", raising=False)
    assert real.work_where() == ("awm", "door-work")
    monkeypatch.setenv("AWM_DOOR_WORK_SCOPE", "elsewhere")
    assert real.work_where() == ("awm", "elsewhere")


# -- a session that is up but cannot work ---------------------------------------

LOGIN = "login required \u2014 run /login"


def modes_started(cx):
    return [s["mode"] for s in cx.started]


async def test_a_representative_blocked_on_login_is_not_alive_and_not_restarted(queue, caplog):
    cx = FakeCx()
    ours(queue, cx, "representative", state="blocked", needs=LOGIN)
    ours(queue, cx, "secretary", job="sec00001", state="done")
    loop = loop_for(cx, queue)
    health = await loop.health()
    assert health["representative"] == {"alive": False, "state": "blocked",
                                        "reason": "auth_required", "job": "abc12345"}
    assert health["secretary"]["alive"] is True
    assert (await loop.alive())["representative"] is False
    with caplog.at_level("WARNING"):
        for _ in range(5):
            await loop.tick()
    assert cx.started == [] and cx.stopped == []  # no restart storm
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "auth_required" in warnings[0].getMessage()
    assert loop.jobs["representative"] == "abc12345"


async def test_a_changed_block_is_warned_about_again_and_recovery_wakes_the_role(queue, caplog):
    cx = FakeCx()
    ours(queue, cx, "representative", state="blocked", needs=LOGIN)
    ours(queue, cx, "secretary", job="sec00001", state="done")
    woke = []
    loop = loop_for(cx, queue, on_started=woke.append)
    with caplog.at_level("INFO"):
        await loop.tick()
        cx.rows[0]["needs"] = "usage limit reached \u2014 check plan"
        await loop.tick()
        assert woke == []
        cx.rows[0]["state"] = "working"
        await loop.tick()
        await loop.tick()
    levels = [(r.levelname, r.getMessage()) for r in caplog.records if "representative" in r.getMessage()]
    assert [lv for lv, _ in levels].count("WARNING") == 2
    assert "usage limit reached" in levels[1][1]
    assert levels[-1] == ("INFO", "door: the representative (abc12345) can work again")
    assert woke == ["representative"]  # once, so queued cards are announced again at once
    assert (await loop.health())["representative"]["state"] == "running"
    assert cx.started == [] and cx.stopped == []


@pytest.mark.parametrize("needs", [
    "org disabled OAuth \u2014 use API key or ask admin",
    "account on hold \u2014 see detail",
    "cloud credentials unavailable \u2014 check or refresh them",
    "organization verification required \u2014 see detail",
    "usage limit reached \u2014 check plan",
])
async def test_a_block_only_a_person_can_clear_is_not_alive_and_keeps_claude_codes_words(queue, needs):
    cx = FakeCx()
    ours(queue, cx, "representative", state="blocked", needs=needs)
    health = (await loop_for(cx, queue).health())["representative"]
    assert (health["alive"], health["state"], health["reason"]) == (False, "blocked", needs)


@pytest.mark.parametrize("needs", [
    "rate limited \u2014 wait and retry",
    "API overloaded \u2014 wait and retry",
    "API unavailable \u2014 retry",
    "request too large \u2014 /compact or trim",
    "which of the two cards first?",   # the session's own closing question
    "blocked: globus login needed",    # the session's own line, not an expired login
    None,
])
async def test_a_block_that_clears_on_the_next_prompt_is_waiting_and_alive(queue, needs):
    cx = FakeCx()
    ours(queue, cx, "representative", state="blocked", needs=needs)
    ours(queue, cx, "secretary", job="sec00001", state="done")
    loop = loop_for(cx, queue)
    health = (await loop.health())["representative"]
    assert (health["alive"], health["state"]) == (True, "waiting")
    assert health["reason"] == (needs or "needs input")
    await loop.tick()
    assert cx.started == [] and cx.stopped == []


async def test_a_login_expired_detail_marks_auth_required_when_needs_is_otherwise(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", state="blocked", needs="API error \u2014 see detail",
         detail="Login expired. Run /login")
    health = (await loop_for(cx, queue).health())["representative"]
    assert (health["alive"], health["reason"]) == (False, "auth_required")


@pytest.mark.parametrize("state", ["working", "done", "idle", "unknown"])
async def test_a_running_session_is_alive_and_untouched(queue, state):
    cx = FakeCx()
    ours(queue, cx, "representative", state=state)
    ours(queue, cx, "secretary", job="sec00001", state=state)
    loop = loop_for(cx, queue)
    await loop.tick()
    await loop.tick()
    assert cx.started == [] and cx.stopped == []
    assert (await loop.health())["representative"] == {
        "alive": True, "state": "running", "reason": "", "job": "abc12345"}


async def test_an_exited_session_is_reported_then_restarted(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", state="gone")
    ours(queue, cx, "secretary", job="sec00001", state="done")
    loop = loop_for(cx, queue)
    health = (await loop.health())["representative"]
    assert (health["alive"], health["state"], health["job"]) == (False, "exited", "abc12345")
    await loop.tick()
    assert modes_started(cx) == ["representative"]
    assert cx.stopped == []
    assert (await loop.health())["representative"]["state"] == "running"


async def test_a_role_the_door_never_started_is_missing(queue):
    health = await loop_for(FakeCx(), queue).health()
    assert health["representative"] == {"alive": False, "state": "missing", "reason": "", "job": None}


async def test_health_says_unknown_when_cx_cannot_answer(queue):
    cx = FakeCx()
    cx.unavailable = True
    health = await loop_for(cx, queue).health()
    assert health["representative"]["alive"] is None
    assert health["representative"]["state"] == "unknown"


async def test_a_failed_session_is_stopped_and_replaced(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", state="failed", needs="API error")
    ours(queue, cx, "secretary", job="sec00001", state="done")
    loop = loop_for(cx, queue)
    assert (await loop.health())["representative"]["reason"] == "API error"
    await loop.tick()
    assert cx.stopped == ["abc12345"]
    assert modes_started(cx) == ["representative"]
    assert queue.session_jobs("representative") == {"abc12345", "job0001"}
    assert loop.jobs["representative"] == "job0001"


async def test_a_replacement_that_fails_again_waits_out_the_back_off(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", state="failed", needs="API error")
    ours(queue, cx, "secretary", job="sec00001", state="done")
    loop = loop_for(cx, queue)
    await loop.tick()
    cx.rows[-1].update(state="failed", needs="API error")  # the replacement fails at once
    for _ in range(4):
        await loop.tick()
    assert cx.stopped == ["abc12345"] and len(cx.started) == 1
    loop._next_try["representative"] = 0.0  # the wait is over
    await loop.tick()
    assert cx.stopped == ["abc12345", "job0001"] and len(cx.started) == 2
    first = loop._next_try["representative"] - time.time()
    assert first > reconcile.MIN_RETRY_S  # doubled


async def test_a_failed_session_cx_will_not_stop_is_not_replaced(queue):
    cx = FakeCx()
    cx.refuse_stop = "cx will not stop it"
    ours(queue, cx, "representative", state="failed", needs="API error")
    ours(queue, cx, "secretary", job="sec00001", state="done")
    loop = loop_for(cx, queue)
    for _ in range(3):
        await loop.tick()
    assert cx.stopped == ["abc12345"] and cx.started == []


async def test_a_failed_line_the_session_wrote_itself_is_not_a_failure_to_restart(queue):
    cx = FakeCx()
    ours(queue, cx, "representative", state="failed", needs="board unreachable for card X")
    ours(queue, cx, "secretary", job="sec00001", state="done")
    loop = loop_for(cx, queue)
    for _ in range(3):
        await loop.tick()
    assert (await loop.health())["representative"]["state"] == "running"
    assert cx.started == [] and cx.stopped == []


async def test_a_blocked_or_failed_session_the_door_did_not_start_is_never_touched(queue):
    cx = FakeCx([session_row("representative", job="evil0001", state="failed"),
                 session_row("secretary", job="evil0002", state="blocked", name="other")])
    await loop_for(cx, queue).tick()
    assert cx.stopped == []
    assert modes_started(cx) == ["representative", "secretary"]
