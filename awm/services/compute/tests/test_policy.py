"""Caps, the pressure gate, and the dwell timers."""

from __future__ import annotations

import pytest

from awm.compute.boxfacts import GIB, Box, Pressure
from awm.compute.policy import Judge, Thresholds, derive_caps
from awm.compute.usage import SessionUsage

pytestmark = pytest.mark.smoke

BOX = Box(nproc=16, mem_total_b=int(68.7 * GIB), swap_total_b=24 * GIB,
          boot_ticks=0)


def usage(sid="s", cores=0.0, gb=0.0, roots=(10,)) -> SessionUsage:
    return SessionUsage(
        session_id=sid, pids=[10], job_roots=list(roots), cpu_cores=cores,
        rss_estimate_b=int(gb * GIB), n_procs=1,
        oldest_start_ticks=0, newest_start_ticks=0,
    )


def pressure(*, avail_gb=50.0, swap_gb=20.0, psi_mem=0.0, psi_cpu=0.0,
             busy=0.0) -> Pressure:
    return Pressure(
        ts=0.0, mem_available_b=int(avail_gb * GIB), mem_free_b=0,
        swap_free_b=int(swap_gb * GIB), psi_mem_some10=psi_mem,
        psi_mem_full10=0.0, psi_cpu_some10=psi_cpu, cpu_busy_cores=busy,
    )


CALM = pressure()
SQUEEZED = pressure(avail_gb=4.0, psi_mem=20.0)


def test_caps_are_derived_from_the_box_not_hardcoded():
    caps = derive_caps(BOX, Thresholds())
    assert caps.hard_cpu_cores == 14.0          # nproc - 2
    assert caps.soft_cpu_cores == 8.0           # half the box
    assert caps.hard_mem_b == pytest.approx((68.7 - 8) * GIB, rel=1e-3)
    assert caps.soft_mem_b == pytest.approx(68.7 / 2 * GIB, rel=1e-3)

    small = derive_caps(Box(nproc=4, mem_total_b=8 * GIB, swap_total_b=0,
                            boot_ticks=0), Thresholds())
    assert small.hard_cpu_cores == 2.0
    assert small.soft_cpu_cores == 2.0          # never above the hard ceiling


def test_a_quiet_box_lets_one_session_exceed_half_the_machine():
    """The freedom half of the brief, stated as a test so it cannot be
    'tidied away' later as an oversight."""
    j = Judge(box=BOX, thresholds=Thresholds())
    for t in (0.0, 100.0):
        v = j.evaluate({"s": usage(cores=12.0, gb=45.0)}, CALM, {}, now=t)
    assert v == []


def test_the_hard_ceiling_applies_even_on_a_quiet_box():
    j = Judge(box=BOX, thresholds=Thresholds())
    j.evaluate({"s": usage(cores=15.0)}, CALM, {}, now=0.0)
    v = j.evaluate({"s": usage(cores=15.0)}, CALM, {}, now=31.0)
    assert [x.metric for x in v] == ["cpu"]
    assert v[0].kind == "hard"


def test_the_soft_cap_goes_live_only_under_pressure():
    j = Judge(box=BOX, thresholds=Thresholds())
    over = {"s": usage(gb=40.0)}                # over 34.4, under 60.7
    assert j.evaluate(over, CALM, {}, now=0.0) == []
    j.evaluate(over, SQUEEZED, {}, now=100.0)
    v = j.evaluate(over, SQUEEZED, {}, now=111.0)
    assert [(x.metric, x.kind) for x in v][0] == ("memory", "soft")


def test_a_session_already_over_the_line_still_gets_its_grace_period():
    """The gotcha this whole timer design exists for.

    A session sitting happily at 40 GiB becomes a violation the moment another
    agent starts and pressure rises. If the clock ran from when it crossed the
    line it would be acted on instantly, having done nothing wrong.
    """
    j = Judge(box=BOX, thresholds=Thresholds())
    over = {"s": usage(gb=40.0)}
    for t in range(0, 600, 60):                 # an hour over the soft cap...
        assert j.evaluate(over, CALM, {}, now=float(t)) == []
    assert j.evaluate(over, SQUEEZED, {}, now=601.0) == []      # ...gate opens
    assert j.evaluate(over, SQUEEZED, {}, now=605.0) == []      # still in grace
    assert j.evaluate(over, SQUEEZED, {}, now=612.0)            # dwell elapsed


def test_a_gate_that_closes_and_reopens_restarts_the_grace_period():
    """Without this, a stale clock from an earlier squeeze fires instantly."""
    j = Judge(box=BOX, thresholds=Thresholds())
    over = {"s": usage(gb=40.0)}
    j.evaluate(over, SQUEEZED, {}, now=0.0)     # gate opens, clock starts
    j.evaluate(over, CALM, {}, now=5.0)         # gate closes before it ripens
    assert j.evaluate(over, SQUEEZED, {}, now=300.0) == []   # reopened: restart
    assert j.evaluate(over, SQUEEZED, {}, now=311.0)


def test_pressure_names_the_largest_contributor_when_nobody_is_over_a_cap():
    """Five sessions at 30% each is the realistic route to a wedged box."""
    j = Judge(box=BOX, thresholds=Thresholds())
    sessions = {f"s{i}": usage(sid=f"s{i}", gb=10.0 + i) for i in range(5)}
    j.evaluate(sessions, SQUEEZED, {}, now=0.0)
    v = j.evaluate(sessions, SQUEEZED, {}, now=11.0)
    assert [(x.session_id, x.kind) for x in v] == [("s4", "pressure")]


def test_pressure_ignores_sessions_too_small_to_blame():
    j = Judge(box=BOX, thresholds=Thresholds())
    tiny = {"s": usage(gb=0.5)}
    j.evaluate(tiny, SQUEEZED, {}, now=0.0)
    assert j.evaluate(tiny, SQUEEZED, {}, now=11.0) == []


def test_psi_alone_does_not_open_the_memory_gate():
    """Measured on this box: PSI spiked to 28% during unrelated heavy work
    while 58 GiB was still available. Pressure alone would misfire."""
    j = Judge(box=BOX, thresholds=Thresholds())
    noisy = pressure(avail_gb=58.0, psi_mem=28.0)
    over = {"s": usage(gb=40.0)}
    j.evaluate(over, noisy, {}, now=0.0)
    assert j.evaluate(over, noisy, {}, now=100.0) == []


def test_a_grant_raises_a_session_ceiling_and_never_lowers_it():
    j = Judge(box=BOX, thresholds=Thresholds())
    over = {"s": usage(gb=40.0)}
    grant = {"s": {"mem_gb": 50.0, "cpu_cores": None}}
    j.evaluate(over, SQUEEZED, grant, now=0.0)
    assert j.evaluate(over, SQUEEZED, grant, now=100.0) == []

    shrinking = {"s": {"mem_gb": 1.0, "cpu_cores": None}}
    j2 = Judge(box=BOX, thresholds=Thresholds())
    j2.evaluate(over, SQUEEZED, shrinking, now=0.0)
    assert j2.evaluate(over, SQUEEZED, shrinking, now=100.0)  # still just soft


def test_hysteresis_keeps_a_timer_until_the_session_is_comfortably_under():
    j = Judge(box=BOX, thresholds=Thresholds())
    j.evaluate({"s": usage(gb=40.0)}, SQUEEZED, {}, now=0.0)
    # Dips just under the cap: the clock must survive, or a job oscillating on
    # the line resets it forever and is never acted on.
    j.evaluate({"s": usage(gb=34.0)}, SQUEEZED, {}, now=3.0)
    assert j._timers
    # Comfortably under, and below the pressure floor too, so nothing keeps it.
    j.evaluate({"s": usage(gb=1.0)}, SQUEEZED, {}, now=4.0)
    assert not j._timers


def test_the_quiet_period_and_its_timer_clear():
    j = Judge(box=BOX, thresholds=Thresholds())
    j.evaluate({"s": usage(gb=70.0)}, SQUEEZED, {}, now=0.0)
    assert j._timers
    j.note_action(10.0)
    assert j.in_quiet_period(20.0)
    assert not j.in_quiet_period(200.0)
    # Cleared, not paused: a second action must not land on an innocent
    # sibling the instant the quiet period lapses.
    assert not j._timers


def test_memory_is_judged_before_cpu():
    j = Judge(box=BOX, thresholds=Thresholds())
    both = {"s": usage(cores=15.0, gb=70.0)}
    j.evaluate(both, SQUEEZED, {}, now=0.0)
    v = j.evaluate(both, SQUEEZED, {}, now=100.0)
    assert [x.metric for x in v] == ["memory", "cpu"]
