"""Reconcile and catch-up, as pure input/output over a real ``dvc_jobs`` table.

No sleeping and no Globus: everything here is "given this schedule state at this
instant, what is due?". The catch-up cases are the ones worth having — they are
this loop's reimplementation of systemd's ``Persistent=true``, and getting them
wrong means either a missed night or a stale backup firing days later.
"""

from __future__ import annotations

from datetime import datetime

from awm.dvc import scheduler as schedmod


def at(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi).timestamp()


def sched(dvc_db, **kw):
    return schedmod.Scheduler(adapter=None, dao=dvc_db, **kw)


def seed(dvc_db, name="cache_sync", *, cron="0 4 * * *", window=20 * 3600):
    dvc_db.seed_job(name, cron=cron, catchup_window_s=window)


# --- ordinary firing --------------------------------------------------------


def test_nothing_is_due_before_its_time(dvc_db):
    seed(dvc_db)
    s = sched(dvc_db)

    assert s._reconcile(at(2026, 8, 13, 3, 0)) == []


def test_a_job_fires_once_when_its_slot_arrives(dvc_db):
    seed(dvc_db)
    s = sched(dvc_db)
    s._reconcile(at(2026, 8, 13, 3, 59))  # prime next_due

    assert s._reconcile(at(2026, 8, 13, 4, 0)) == [("cache_sync", "schedule")]
    # and the same slot is not served twice
    assert s._reconcile(at(2026, 8, 13, 4, 0)) == []


def test_waking_late_fires_once_and_skips_ahead(dvc_db):
    """Next-due is recomputed from `now`, so a slow tick is not a replay storm."""
    seed(dvc_db, cron="@every 1m")
    s = sched(dvc_db)
    s._reconcile(1000.0)

    due = s._reconcile(1000.0 + 3600)

    assert due == [("cache_sync", "schedule")]
    assert s.next_due("cache_sync") == 1000.0 + 3600 + 60


def test_a_disabled_job_is_never_due(dvc_db):
    seed(dvc_db)
    dvc_db.set_schedule("cache_sync", enabled=False)
    s = sched(dvc_db)

    assert s._reconcile(at(2026, 8, 13, 4, 0)) == []
    assert s.next_due("cache_sync") is None


def test_a_changed_cron_is_picked_up_without_a_restart(dvc_db):
    seed(dvc_db)
    s = sched(dvc_db)
    s._reconcile(at(2026, 8, 13, 3, 0))
    before = s.next_due("cache_sync")

    dvc_db.set_schedule("cache_sync", cron="0 9 * * *")
    s._reconcile(at(2026, 8, 13, 3, 0))

    assert s.next_due("cache_sync") != before
    assert datetime.fromtimestamp(s.next_due("cache_sync")).hour == 9


def test_a_bad_cron_is_dropped_not_raised(dvc_db):
    """The loop must survive a spec someone wrote by hand into the table."""
    seed(dvc_db, cron="not a cron")
    s = sched(dvc_db)

    assert s._reconcile(at(2026, 8, 13, 4, 0)) == []
    assert s.next_due("cache_sync") is None


def test_the_sleep_is_capped_so_a_schedule_edit_lands_promptly(dvc_db):
    seed(dvc_db)
    s = sched(dvc_db)
    now = at(2026, 8, 13, 3, 0)
    s._reconcile(now)

    assert s._sleep_for(now) == schedmod.POLL_MAX_S
    # ...but it never overshoots an imminent slot.
    assert s._sleep_for(s.next_due("cache_sync") - 1) == 1.0


# --- catch-up ---------------------------------------------------------------


def test_a_slot_missed_while_down_fires_once_on_the_next_start(dvc_db):
    seed(dvc_db)
    dvc_db.set_last_fire("cache_sync", at(2026, 8, 12, 4, 0))
    s = sched(dvc_db)

    # Back up at 09:00 having missed the 04:00 slot.
    due = s._reconcile(at(2026, 8, 13, 9, 0))

    assert ("cache_sync", "catchup") in due
    # Only on the first reconcile — a second pass must not replay it.
    assert s._reconcile(at(2026, 8, 13, 9, 0) + 1) == []


def test_a_machine_off_for_a_week_does_not_fire_a_stale_backup(dvc_db):
    seed(dvc_db)
    dvc_db.set_last_fire("cache_sync", at(2026, 8, 5, 4, 0))
    s = sched(dvc_db)

    assert s._reconcile(at(2026, 8, 13, 9, 0)) == []


def test_catch_up_is_bounded_by_the_window_not_by_the_day(dvc_db):
    seed(dvc_db, window=3600)  # one hour
    dvc_db.set_last_fire("cache_sync", at(2026, 8, 12, 4, 0))
    s = sched(dvc_db)

    # 04:30 — the 04:00 slot was missed half an hour ago, inside the window.
    assert ("cache_sync", "catchup") in sched(dvc_db)._reconcile(at(2026, 8, 13, 4, 30))
    # 06:00 — two hours late, outside it.
    assert s._reconcile(at(2026, 8, 13, 6, 0)) == []


def test_a_job_that_never_fired_has_nothing_to_catch_up(dvc_db):
    """A fresh install must not submit a backup the instant it starts."""
    seed(dvc_db)
    s = sched(dvc_db)

    assert s._reconcile(at(2026, 8, 13, 9, 0)) == []


def test_health_reports_a_loop_that_has_never_ticked_as_stopped(dvc_db):
    s = sched(dvc_db)

    assert s.health()["stopped"] is True
    assert s.health()["ticks"] == 0
