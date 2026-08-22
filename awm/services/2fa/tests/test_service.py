"""Service-runtime tests — verb wiring + device routing + the counted/overlapping
burst task — with fake engines, so nothing touches Duo or the network."""

from __future__ import annotations

import asyncio
import types

import pytest

from awm.twofa.config import Config, DeviceCreds
from awm.twofa.service import DeviceRuntime, TwoFAService


def _creds(tmp_path, name: str) -> DeviceCreds:
    return DeviceCreds(name, tmp_path / f"{name}-creds.json", tmp_path / f"{name}-key.pem")


def empty_cfg(tmp_path, *names: str) -> Config:
    """A Config with the named devices, none enrolled (no cred files on disk)."""
    names = names or ("cwl",)
    return Config(devices={n: _creds(tmp_path, n) for n in names})


class FakeEngine:
    def __init__(self, txs_per_poll=None) -> None:
        self.approved_count = 0
        self.calls: list[tuple] = []
        self._budget = 0
        # Optional script: a list of tx-lists returned by successive polls.
        self._script = list(txs_per_poll or [])
        self.client = types.SimpleNamespace(
            host="api-test.duosecurity.com",
            get_transactions=self._next_poll,
        )

    def _next_poll(self):
        return self._script.pop(0) if self._script else []

    # Budget API mirroring ApprovalEngine.
    def grant(self, n):
        self._budget += max(0, int(n))
        return self._budget

    def budget_remaining(self):
        return self._budget

    def clear_budget(self):
        n = self._budget
        self._budget = 0
        return n

    def held_transactions(self):
        return []

    def approve_all_remaining(self):
        return 0.0

    def approve(self, urgid, by="awm"):
        self.calls.append(("approve", urgid))
        self.approved_count += 1
        return True

    def deny(self, urgid, by="awm"):
        self.calls.append(("deny", urgid))
        return True

    def approve_all(self, by="awm"):
        self.calls.append(("approve_all",))
        return 3

    def handle_transactions(self, txs):
        self.calls.append(("handle", len(txs)))
        # Budget-driven, like the real engine: approve up to the granted budget.
        n = min(len(txs), self._budget)
        self.approved_count += n
        self._budget -= n


def inject(svc: TwoFAService, name: str, eng: FakeEngine) -> DeviceRuntime:
    """Pre-seed a device runtime's cached engine so _load_engine returns it
    without reading creds off disk."""
    rt = svc._devices[name]
    rt.engine = eng
    rt.client = eng.client
    return rt


# ---- device routing / aggregation -----------------------------------------

@pytest.mark.smoke
def test_unenrolled_reports_cleanly(tmp_path):
    svc = TwoFAService(empty_cfg(tmp_path, "cwl", "alliance"))
    svc.init()  # must not raise without creds
    ping = asyncio.run(svc.ping())
    assert ping["devices"]["cwl"]["enrolled"] is False
    assert ping["devices"]["alliance"]["enrolled"] is False
    # ok must reflect a VERIFIED Duo round-trip, never local state alone —
    # `ok: true, enrolled: true` with the API unreachable is exactly the false
    # green that made the approver look healthy on 2026-07-26.
    assert ping["ok"] is False
    assert ping["devices"]["cwl"]["reachable"] is False
    st = svc.status()
    assert set(st["devices"]) == {"cwl", "alliance"}
    assert st["devices"]["cwl"]["enrolled"] is False
    assert svc.pending()["devices"]["cwl"]["held"] == []


@pytest.mark.smoke
def test_action_verbs_require_device(tmp_path):
    svc = TwoFAService(empty_cfg(tmp_path, "cwl"))
    inject(svc, "cwl", FakeEngine())
    with pytest.raises(ValueError):
        svc.approve("u1", "")
    with pytest.raises(ValueError):
        svc.deny("u1", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        svc.approve_all("")


@pytest.mark.smoke
def test_unknown_device_raises(tmp_path):
    svc = TwoFAService(empty_cfg(tmp_path, "cwl"))
    inject(svc, "cwl", FakeEngine())
    with pytest.raises(ValueError):
        svc.approve("u1", "nope")


@pytest.mark.smoke
def test_approve_deny_route_to_named_device(tmp_path):
    svc = TwoFAService(empty_cfg(tmp_path, "cwl", "alliance"))
    cwl = FakeEngine()
    alliance = FakeEngine()
    inject(svc, "cwl", cwl)
    inject(svc, "alliance", alliance)

    assert svc.approve("u1", "cwl") == {"ok": True, "device": "cwl", "urgid": "u1"}
    assert svc.deny("u2", "alliance")["device"] == "alliance"
    assert svc.approve_all("cwl")["cleared"] == 3

    # Each engine only saw its own call.
    assert ("approve", "u1") in cwl.calls and ("approve", "u1") not in alliance.calls
    assert ("deny", "u2") in alliance.calls and ("deny", "u2") not in cwl.calls


@pytest.mark.smoke
def test_status_single_vs_aggregate(tmp_path):
    cfg = empty_cfg(tmp_path, "cwl", "alliance")
    # Touch cwl's cred files so it reports enrolled.
    cfg.devices["cwl"].creds_path.write_text("{}")
    cfg.devices["cwl"].key_path.write_text("x")
    svc = TwoFAService(cfg)
    inject(svc, "cwl", FakeEngine())

    one = svc.status("cwl")
    assert one["device"] == "cwl"
    assert one["enrolled"] is True
    assert one["host"] == "api-test.duosecurity.com"

    agg = svc.status()
    assert agg["devices"]["cwl"]["enrolled"] is True
    assert agg["devices"]["alliance"]["enrolled"] is False


# ---- counted / overlapping burst ------------------------------------------

@pytest.mark.smoke
async def test_burst_started_then_extended(tmp_path):
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl")},
                 burst_window_seconds=0.4, burst_interval_seconds=0.05)
    svc = TwoFAService(cfg)
    inject(svc, "cwl", FakeEngine())  # returns no txs, so it won't auto-finish

    r1 = await svc.start_burst("cwl", count=1)
    assert r1["status"] == "started"
    assert r1["expected"] == 1
    rt = svc._devices["cwl"]
    assert rt.burst_active() is True
    task = rt.burst_task

    r2 = await svc.start_burst("cwl", count=1)
    assert r2["status"] == "extended"
    assert r2["expected"] == 2
    # Same single task throughout — overlapping bursts don't spawn a second.
    assert rt.burst_task is task

    await asyncio.sleep(0.5)
    assert rt.burst_active() is False
    assert rt.last_burst is not None


@pytest.mark.smoke
async def test_burst_devices_independent(tmp_path):
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl"),
                          "alliance": _creds(tmp_path, "alliance")},
                 burst_window_seconds=0.4, burst_interval_seconds=0.05)
    svc = TwoFAService(cfg)
    inject(svc, "cwl", FakeEngine())
    inject(svc, "alliance", FakeEngine())

    await svc.start_burst("cwl")
    assert svc._devices["cwl"].burst_active() is True
    # alliance untouched by the cwl burst.
    assert svc._devices["alliance"].burst_active() is False
    assert svc._devices["cwl"].burst_task is not svc._devices["alliance"].burst_task

    await svc.start_burst("alliance")
    assert svc._devices["alliance"].burst_active() is True

    await asyncio.sleep(0.5)
    assert svc._devices["cwl"].burst_active() is False
    assert svc._devices["alliance"].burst_active() is False


@pytest.mark.smoke
async def test_burst_exits_when_counter_satisfied(tmp_path):
    # A long window, but two polls each surface one tx the fake engine approves,
    # so expected (2) reaches 0 and the loop ends well before the deadline.
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl")},
                 burst_window_seconds=30.0, burst_interval_seconds=0.02)
    svc = TwoFAService(cfg)
    from awm.twofa.duo import Transaction
    eng = FakeEngine(txs_per_poll=[
        [Transaction(urgid="a", raw={})],
        [Transaction(urgid="b", raw={})],
    ])
    inject(svc, "cwl", eng)

    await svc.start_burst("cwl", count=2)
    rt = svc._devices["cwl"]
    # Wait for the counted exit (far below the 30s window).
    for _ in range(100):
        await asyncio.sleep(0.02)
        if not rt.burst_active():
            break
    assert rt.burst_active() is False
    assert eng.approved_count == 2
    assert rt.last_burst == {"approved": 2, "expected_remaining": 0}


@pytest.mark.smoke
async def test_burst_notify_on_approval_and_window_end(tmp_path):
    # One poll surfaces a tx the fake engine approves; with count=1 the window
    # then ends. Expect a per-approval notify AND a window-end summary.
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl")},
                 burst_window_seconds=30.0, burst_interval_seconds=0.02)
    svc = TwoFAService(cfg)
    from awm.twofa.duo import Transaction
    eng = FakeEngine(txs_per_poll=[[Transaction(urgid="a", raw={})]])
    inject(svc, "cwl", eng)

    msgs: list[str] = []

    async def notify(text):
        msgs.append(text)

    await svc.start_burst("cwl", count=1, notify=notify)
    rt = svc._devices["cwl"]
    for _ in range(200):
        await asyncio.sleep(0.02)
        if not rt.burst_active():
            break
    await asyncio.sleep(0.05)  # let the fire-and-forget end-summary task run

    assert any("approved on" in m for m in msgs)   # per-approval line
    assert any("window ended" in m for m in msgs)  # end-of-window summary


@pytest.mark.smoke
async def test_concurrent_same_device_burst_spawns_one_task(tmp_path):
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl")},
                 burst_window_seconds=0.4, burst_interval_seconds=0.05)
    svc = TwoFAService(cfg)
    inject(svc, "cwl", FakeEngine())

    results = await asyncio.gather(
        svc.start_burst("cwl"), svc.start_burst("cwl"), svc.start_burst("cwl"))
    statuses = sorted(r["status"] for r in results)
    assert statuses.count("started") == 1
    assert statuses.count("extended") == 2
    rt = svc._devices["cwl"]
    # Three overlapping arms accumulate into one engine budget.
    assert rt.engine.budget_remaining() == 3

    await asyncio.sleep(0.5)
    assert rt.burst_active() is False


@pytest.mark.smoke
async def test_rearm_after_teardown_spawns_fresh_task(tmp_path):
    """After a window fully tears down (task cleared), a new start_burst must
    spawn a FRESH task ('started', not 'extended') and honour its budget. Guards
    the was_active→live-check fix: a dead task must not be treated as active,
    which would strand the new grant with no poll loop (the original bug)."""
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl")},
                 burst_window_seconds=0.1, burst_interval_seconds=0.02)
    svc = TwoFAService(cfg)
    inject(svc, "cwl", FakeEngine())  # no txs → runs to the deadline, then tears down

    r1 = await svc.start_burst("cwl", count=1)
    assert r1["status"] == "started"
    rt = svc._devices["cwl"]
    for _ in range(100):
        await asyncio.sleep(0.02)
        if not rt.burst_active():
            break
    assert rt.burst_active() is False
    assert rt.burst_task is None                 # teardown detached the task
    assert rt.engine.budget_remaining() == 0     # and cleared leftover budget

    # Fresh arm after teardown → a NEW task, budget honoured (not stranded).
    r2 = await svc.start_burst("cwl", count=2)
    assert r2["status"] == "started"
    assert r2["expected"] == 2
    assert rt.burst_active() is True
    assert rt.engine.budget_remaining() == 2
    await asyncio.sleep(0.25)
    assert rt.burst_active() is False


@pytest.mark.smoke
async def test_rearm_during_exit_is_not_stranded(tmp_path):
    """The teardown/re-arm race: a start_burst that lands as the poll loop is
    exiting (budget just hit 0) must keep a live task that observes the new grant,
    never a cleared budget with no poller. Driven deterministically by exhausting
    the budget on the first poll, then re-arming immediately."""
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl")},
                 burst_window_seconds=5.0, burst_interval_seconds=0.02)
    svc = TwoFAService(cfg)
    from awm.twofa.duo import Transaction
    # First poll surfaces one tx the engine approves → budget 1→0, loop heads to
    # its exit/coordination check.
    eng = FakeEngine(txs_per_poll=[[Transaction(urgid="a", raw={})]])
    inject(svc, "cwl", eng)

    await svc.start_burst("cwl", count=1)
    rt = svc._devices["cwl"]
    # Re-arm right away, while the (long-window) task is still alive around its
    # exit check. It must stay the same single task and carry the new budget.
    r2 = await svc.start_burst("cwl", count=1)
    assert r2["status"] == "extended"
    assert rt.burst_active() is True
    assert rt.engine.budget_remaining() >= 1  # new grant retained, not cleared


# ---- reachability: ping must not be able to lie ---------------------------

def _enrolled_cfg(tmp_path, *names: str) -> Config:
    """Like :func:`empty_cfg`, but with cred files on disk so the devices read
    as enrolled — reachability is only meaningful for an enrolled device."""
    cfg = empty_cfg(tmp_path, *names)
    for creds in cfg.devices.values():
        creds.creds_path.write_text("{}")
        creds.key_path.write_text("x")
    return cfg


class _ReachableClient:
    host = "api-test.duosecurity.com"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def get_transactions(self, timeout=None):
        self.calls += 1
        if self.fail:
            raise OSError("Temporary failure in name resolution")
        return []


@pytest.mark.smoke
def test_ping_reports_unreachable_when_duo_cannot_be_reached(tmp_path):
    """The exact 2026-07-26 shape: creds on disk, device enrolled, Duo API
    unresolvable. ping used to answer ok/enrolled from local state and say
    nothing about the network."""
    svc = TwoFAService(_enrolled_cfg(tmp_path, "alliance"))
    eng = FakeEngine()
    eng.client = _ReachableClient(fail=True)
    inject(svc, "alliance", eng)

    res = asyncio.run(svc.ping("alliance"))
    assert res["ok"] is False
    assert res["reachable"] is False
    assert "resolution" in res["error"]
    # And nothing may claim a verified round-trip that never happened.
    assert svc.reachability("alliance")["last_reachable_at"] is None


@pytest.mark.smoke
def test_ping_records_a_verified_round_trip(tmp_path):
    svc = TwoFAService(_enrolled_cfg(tmp_path, "alliance"))
    eng = FakeEngine()
    eng.client = _ReachableClient()
    inject(svc, "alliance", eng)

    res = asyncio.run(svc.ping("alliance"))
    assert res["ok"] is True and res["reachable"] is True
    assert eng.client.calls == 1          # a real call, not a local guess
    assert svc.reachability("alliance")["last_reachable_at"] is not None


@pytest.mark.smoke
def test_reachability_of_an_unknown_device_is_not_a_positive(tmp_path):
    svc = TwoFAService(empty_cfg(tmp_path, "alliance"))
    out = svc.reachability("nosuchdevice")
    assert out["known"] is False
    assert out["last_reachable_at"] is None
