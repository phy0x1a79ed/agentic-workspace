import pytest

from awm.rlm_factorio import throttle


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def make(rate=10.0, burst=20.0, floor=55.0):
    clock = FakeClock()
    return throttle.Throttle(rate, burst, floor, clock=clock, sleep=clock.sleep), clock


def test_debt_delays_until_refilled():
    t, clock = make()
    t.admit("a", clock() + 60)
    t.charge("a", 50.0)                  # 20 - 50 = -30 ms of debt
    held = t.admit("a", clock() + 60)
    assert held == pytest.approx(3.0, abs=0.1)   # 30 ms at 10 ms/s


def test_other_keys_unaffected_by_debt():
    t, clock = make()
    t.admit("a", clock() + 60)
    t.charge("a", 500.0)
    assert t.admit("b", clock() + 60) == 0.0


def test_deadline_raises_and_nothing_charged():
    t, clock = make()
    t.admit("a", clock() + 60)
    t.charge("a", 500.0)
    with pytest.raises(throttle.Throttled, match="Nothing ran"):
        t.admit("a", clock() + 1)
    assert t.snapshot()["keys"]["a"]["queued"] == 0


def test_low_ups_admits_only_quiet_keys():
    t, clock = make()
    for i in range(6):                   # 40 UPS
        t.note_tick(1000 + 40 * i, False)
        clock.t += 1.0
    assert t.ups() == pytest.approx(40.0)
    t.admit("quiet", clock() + 60)
    t.charge("quiet", 1.0)
    assert t.admit("quiet", clock() + 60) == 0.0
    t.charge("busy", 15.0)               # level 5, under half a burst
    assert t.admit("busy", clock() + 60) > 0


def test_paused_disables_ups_gate():
    t, clock = make()
    for i in range(6):
        t.note_tick(1000, True)
        clock.t += 1.0
    assert t.ups() is None


def test_command_budget_clamps():
    assert throttle.command_budget(None) == throttle.CMD_BUDGET_MS
    assert throttle.command_budget(1) == throttle.CMD_BUDGET_MS
    assert throttle.command_budget(10_000) == throttle.CMD_BUDGET_MAX_MS


def test_over_budget_message_forbids_retry():
    msg = throttle.over_budget_message(22.5, 15)
    assert msg.startswith("OVER_BUDGET: THIS COMMAND RAN; DO NOT RETRY")
    assert "22.5" in msg


def test_snapshot_reports_meters():
    t, clock = make()
    t.admit("a", clock() + 60)
    t.charge("a", 30.0)
    t.note_over_budget("a")
    snap = t.snapshot()["keys"]["a"]
    assert snap["debt_ms"] == 10.0 and snap["over_budget"] == 1 and snap["commands"] == 1
