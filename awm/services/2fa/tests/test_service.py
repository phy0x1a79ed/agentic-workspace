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


# ---- counted / overlapping burst ------------------------------------------

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
def test_reachability_of_an_unknown_device_is_not_a_positive(tmp_path):
    svc = TwoFAService(empty_cfg(tmp_path, "alliance"))
    out = svc.reachability("nosuchdevice")
    assert out["known"] is False
    assert out["last_reachable_at"] is None


# --- observation counting + reachability from real traffic --------------------
#
# Two facts the ssh breaker now depends on. Before them, "approved 0 login(s)"
# was the only trace a window left, and it reads the same whether Duo was never
# presented with a login or was and we declined — while `reachability` moved only
# when somebody called `ping`, so it decayed with idle time and made a quiet
# approver look like a broken one.
