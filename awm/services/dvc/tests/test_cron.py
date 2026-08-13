"""The vendored cron parser.

Vendored from awm-events rather than imported (see the module docstring), so it
gets its own tests here — a copy with no tests is a copy that silently rots.
Kept to the forms this service actually uses: a daily cron, the ``@every`` form
that makes a five-minute live drill possible, and the failure modes the
scheduler and the ``schedule`` verb both have to survive.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from awm.dvc import cron


def at(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi).timestamp()


def test_a_daily_cron_lands_on_the_next_occurrence():
    nxt = cron.next_due("0 4 * * *", after=at(2026, 8, 13, 5, 0))

    assert datetime.fromtimestamp(nxt) == datetime(2026, 8, 14, 4, 0)


def test_a_daily_cron_later_the_same_day_stays_that_day():
    nxt = cron.next_due("30 5 * * *", after=at(2026, 8, 13, 4, 15))

    assert datetime.fromtimestamp(nxt) == datetime(2026, 8, 13, 5, 30)


def test_next_due_is_strictly_after_so_a_fire_cannot_refire_itself():
    """The scheduler recomputes from `now` at fire time; equality would loop."""
    now = at(2026, 8, 13, 4, 0)

    assert cron.next_due("0 4 * * *", after=now) > now


def test_the_interval_form_is_relative_to_the_previous_fire():
    assert cron.next_due("@every 5m", after=1000.0) == 1300.0
    assert cron.next_due("@every 30s", after=1000.0) == 1030.0
    assert cron.next_due("@every 2h", after=1000.0) == 8200.0


@pytest.mark.parametrize(
    "spec", ["", "0 4 * *", "nonsense", "60 4 * * *", "@every 0m", "@every 5d"]
)
def test_a_malformed_spec_raises_rather_than_being_quietly_ignored(spec):
    with pytest.raises(cron.CronError):
        cron.validate(spec)


def test_the_two_default_schedules_parse():
    """Guards the constants in jobs.py, which nothing else would catch."""
    from awm.dvc import jobs

    for spec in jobs.JOBS.values():
        cron.validate(spec.cron)
