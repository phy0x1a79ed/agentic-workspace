"""Unit tests for the events service: cron parsing, schedule CRUD, and the
scheduler's reconcile/fire logic. All deterministic — no real-time sleeping.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from awm.events import cron
from awm.events.dao import EventsDAO, init


# --------------------------------------------------------------------------- #
# cron / interval parsing
# --------------------------------------------------------------------------- #

class TestCron:
    def test_interval_seconds(self):
        assert cron.interval_seconds("@every 30s") == 30
        assert cron.interval_seconds("@every 5m") == 300
        assert cron.interval_seconds("@every 2h") == 7200
        assert cron.interval_seconds("*/5 * * * *") is None

    def test_interval_next_due_is_relative(self):
        assert cron.next_due("@every 10s", after=1000.0) == 1010.0
        assert cron.next_due("@every 1m", after=1000.0) == 1060.0

    def test_interval_must_be_positive(self):
        with pytest.raises(cron.CronError):
            cron.next_due("@every 0s", after=0.0)

    def test_cron_step_aligns_to_minute(self):
        nxt = cron.next_due("*/5 * * * *", after=1_000_000.0)
        dt = datetime.fromtimestamp(nxt)
        assert nxt > 1_000_000.0
        assert dt.second == 0
        assert dt.minute % 5 == 0

    def test_cron_top_of_hour(self):
        nxt = cron.next_due("0 * * * *", after=1_000_000.0)
        dt = datetime.fromtimestamp(nxt)
        assert dt.minute == 0 and dt.second == 0

    def test_cron_list_and_range(self):
        # Minutes 10,20,30 only.
        nxt = cron.next_due("10,20,30 * * * *", after=1_000_000.0)
        assert datetime.fromtimestamp(nxt).minute in {10, 20, 30}
        # Range 0-2 in the hour field.
        nxt2 = cron.next_due("0 0-2 * * *", after=1_000_000.0)
        assert datetime.fromtimestamp(nxt2).hour in {0, 1, 2}

    def test_cron_strictly_after(self):
        # A spec that matches every minute still returns a *future* minute.
        nxt = cron.next_due("* * * * *", after=1_000_000.0)
        assert nxt >= 1_000_000.0 + 1  # at least the next minute boundary

    @pytest.mark.parametrize("bad", [
        "", "* * *", "* * * * * *", "60 * * * *", "* 24 * * *",
        "abc * * * *", "*/0 * * * *", "5-2 * * * *",
    ])
    def test_validate_rejects_bad(self, bad):
        with pytest.raises(cron.CronError):
            cron.validate(bad)


# --------------------------------------------------------------------------- #
# DAO CRUD round-trips (against a tmp per-service DB)
# --------------------------------------------------------------------------- #

class TestDAO:
    def test_schedule_list_unschedule(self, awm_workspace):
        init()
        dao = EventsDAO()
        assert dao.list_schedules() == []

        dao.schedule("tetris", "@every 30s")
        dao.schedule("chess", "*/5 * * * *")
        games = {r["game"]: r["cron"] for r in dao.list_schedules()}
        assert games == {"tetris": "@every 30s", "chess": "*/5 * * * *"}

        # Re-scheduling overwrites the cron (idempotent upsert).
        dao.schedule("tetris", "@every 1m")
        games = {r["game"]: r["cron"] for r in dao.list_schedules()}
        assert games["tetris"] == "@every 1m"

        assert dao.unschedule("tetris") is True
        assert dao.unschedule("tetris") is False  # already gone
        assert [r["game"] for r in dao.list_schedules()] == ["chess"]

    def test_schedule_rejects_bad_cron(self, awm_workspace):
        init()
        dao = EventsDAO()
        with pytest.raises(ValueError):
            dao.schedule("x", "not a cron")
        with pytest.raises(ValueError):
            dao.schedule("", "@every 5s")
        assert dao.list_schedules() == []

    def test_persists_across_dao_instances(self, awm_workspace):
        init()
        EventsDAO().schedule("demo", "@every 5s")
        # A fresh DAO (mimicking a service restart) reads the same DB.
        again = EventsDAO().list_schedules()
        assert [r["game"] for r in again] == ["demo"]


# --------------------------------------------------------------------------- #
# Scheduler reconcile / fire (no sleeping)
# --------------------------------------------------------------------------- #

class _FakeAdapter:
    def __init__(self):
        self.emitted = []

    async def emit(self, topic, payload):
        self.emitted.append((topic, payload))


class TestScheduler:
    def _sched(self, awm_workspace):
        from awm.events.scheduler import Scheduler
        init()
        dao = EventsDAO()
        return Scheduler(_FakeAdapter(), dao=dao, jitter_s=0.0), dao

    def test_reconcile_due_detection(self, awm_workspace):
        sched, dao = self._sched(awm_workspace)
        dao.schedule("demo", "@every 10s")

        # First pass at t0 computes next_due = t0 + 10 → not due yet.
        assert sched._reconcile(1000.0) == []
        assert sched._next_due["demo"] == 1010.0

        # At the due time it fires and advances.
        assert sched._reconcile(1010.0) == ["demo"]
        assert sched._next_due["demo"] == 1020.0
        # Immediately after, not due again.
        assert sched._reconcile(1011.0) == []

    def test_reconcile_drops_removed(self, awm_workspace):
        sched, dao = self._sched(awm_workspace)
        dao.schedule("demo", "@every 10s")
        sched._reconcile(1000.0)
        assert "demo" in sched._next_due

        dao.unschedule("demo")
        assert sched._reconcile(2000.0) == []
        assert sched._next_due == {}

    def test_reconcile_recomputes_changed_spec(self, awm_workspace):
        sched, dao = self._sched(awm_workspace)
        dao.schedule("demo", "@every 10s")
        sched._reconcile(1000.0)
        assert sched._next_due["demo"] == 1010.0

        dao.schedule("demo", "@every 100s")  # change cadence
        sched._reconcile(1000.0)
        assert sched._next_due["demo"] == 1100.0

    def test_sleep_for_capped(self, awm_workspace):
        sched, dao = self._sched(awm_workspace)
        # No schedules → poll cap.
        from awm.events.scheduler import POLL_MAX_S
        assert sched._sleep_for(1000.0) == POLL_MAX_S
        dao.schedule("demo", "@every 10s")
        sched._reconcile(1000.0)
        # Soonest due is 10s out, but capped to POLL_MAX_S.
        assert sched._sleep_for(1000.0) == POLL_MAX_S

    async def test_fire_emits_tick(self, awm_workspace):
        sched, dao = self._sched(awm_workspace)
        await sched._fire("demo")
        assert sched.adapter.emitted == [("schedule.tick", {"game": "demo"})]
