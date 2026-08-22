"""CPU accounting — the part that is blind if you get it wrong."""

from __future__ import annotations

import pytest

from awm.compute.probe import CLK_TCK
from awm.compute.usage import UsageTracker, subtree

from tests.conftest import mkproc, tree

pytestmark = pytest.mark.smoke


def _sids(procs, sid="s"):
    return {pid: sid for pid in procs}


def test_own_cpu_is_measured_as_a_rate():
    trk = UsageTracker()
    p = mkproc(10, ppid=1)
    trk.update(tree(p), _sids({10: p}), uptime_ticks=1000, now=0.0)

    p2 = mkproc(10, ppid=1, utime=CLK_TCK * 2)  # 2 s of CPU
    u = trk.update(tree(p2), _sids({10: p2}), uptime_ticks=1100, now=1.0)
    assert u["s"].cpu_cores == pytest.approx(2.0)


def test_reaped_children_are_the_whole_point():
    """A parallel build whose children are born and reaped between samples has
    no *own* CPU at all. If ``cutime`` is not counted, it is invisible."""
    trk = UsageTracker()
    make = mkproc(10, ppid=1)
    trk.update(tree(make), _sids({10: make}), uptime_ticks=1000, now=0.0)

    # 8 cores' worth of compiles, all of them already reaped.
    after = mkproc(10, ppid=1, cutime=CLK_TCK * 8)
    u = trk.update(tree(after), _sids({10: after}), uptime_ticks=1100, now=1.0)
    assert u["s"].cpu_cores == pytest.approx(8.0)


def test_a_dying_child_is_not_double_counted():
    """When a child dies its whole lifetime folds into the parent's ``cutime``.

    Without the departed-process correction that shows up as a spike of CPU the
    session never spent in this interval — and on a busy box a spike is exactly
    what triggers an action.
    """
    trk = UsageTracker()
    parent = mkproc(10, ppid=1)
    child = mkproc(11, ppid=10, start=150, utime=CLK_TCK * 5)  # 5 s already
    procs = tree(parent, child)
    trk.update(procs, _sids(procs), uptime_ticks=1000, now=0.0)

    # Child exits having done one more second; parent reaps all 6 s.
    parent2 = mkproc(10, ppid=1, cutime=CLK_TCK * 6)
    u = trk.update(tree(parent2), _sids({10: parent2}), uptime_ticks=1100, now=1.0)
    # 1 s of real work in a 1 s interval — not 6.
    assert u["s"].cpu_cores == pytest.approx(1.0)


def test_a_long_running_process_first_seen_now_does_not_look_like_a_hog():
    """At startup every process has hours of lifetime CPU on the clock.

    Counting it would make the watchdog's first pass condemn whatever has been
    running longest, which is the opposite of what it is for.
    """
    trk = UsageTracker()
    old = mkproc(10, ppid=1, start=5, utime=CLK_TCK * 3600)
    u = trk.update(tree(old), _sids({10: old}), uptime_ticks=1000, now=1.0)
    assert u["s"].cpu_cores == 0.0


def test_a_process_born_inside_the_interval_counts_fully():
    trk = UsageTracker()
    trk.update({}, {}, uptime_ticks=1000, now=0.0)
    born = mkproc(10, ppid=1, start=1050, utime=CLK_TCK)
    u = trk.update(tree(born), _sids({10: born}), uptime_ticks=1100, now=1.0)
    assert u["s"].cpu_cores == pytest.approx(1.0)


def test_job_roots_are_the_shallowest_processes_in_the_session():
    procs = tree(
        mkproc(1, ppid=0),                 # not in the session
        mkproc(10, ppid=1), mkproc(11, ppid=10, start=150),
        mkproc(20, ppid=1),                # a second, detached job
    )
    sids = {10: "s", 11: "s", 20: "s", 1: None}
    u = UsageTracker().update(procs, sids, uptime_ticks=1000, now=1.0)
    assert u["s"].job_roots == [10, 20]


def test_subtree_is_inclusive_and_terminates_on_cycles():
    procs = tree(mkproc(10, ppid=1), mkproc(11, ppid=10), mkproc(12, ppid=11))
    assert {p.pid for p in subtree(10, procs)} == {10, 11, 12}
    assert {p.pid for p in subtree(11, procs)} == {11, 12}

    cyc = tree(mkproc(10, ppid=11), mkproc(11, ppid=10))
    assert {p.pid for p in subtree(10, cyc)} == {10, 11}
