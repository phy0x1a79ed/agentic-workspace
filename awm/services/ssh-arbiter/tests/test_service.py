from __future__ import annotations

import json
from pathlib import Path

import pytest

from awm.ssh.config import resolve_host
from awm.ssh_arbiter import hub_adapter
from awm.ssh_arbiter import service as arbiter
from awm.ssh_arbiter.service import SSHArbiter, lock_path


class _FakeBridge:
    """``client_frames`` are what the requester sends. When they run out, the socket closes."""

    def __init__(self, client_frames):
        self.sent = []
        self._frames = list(client_frames)
        self.closed = False

    async def send(self, raw):
        self.sent.append(raw)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class _FakeCtx:
    def __init__(self, host, bridge, node=None):
        self.init = {"host": host, **({"node": node} if node else {})}
        self._bridge = bridge

    async def open_bridge(self):
        return self._bridge


@pytest.fixture(autouse=True)
def locks(tmp_path, monkeypatch):
    async def _refuse(*_a, **_kw):
        raise RuntimeError("test tried to reach the live gateway")

    monkeypatch.setattr(arbiter.gatewayclient, "call", _refuse)
    monkeypatch.setattr(arbiter, "LOCK_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def svc():
    return SSHArbiter()


@pytest.fixture
def alerts(svc, monkeypatch):
    sent: list[str] = []

    async def _capture(text):
        sent.append(text)

    monkeypatch.setattr(svc, "_alert", _capture)
    return sent


FIR = resolve_host("fir")


def _answer_2fa(monkeypatch, *, ping):
    async def _call(service, verb, args=None, **_kw):
        return ping if verb == "ping" else {"ok": True}

    monkeypatch.setattr(arbiter.gatewayclient, "call", _call)


def test_only_status_and_the_wire_tests_are_verbs():
    # An agent must never hold a verb that lifts a hold.
    assert set(hub_adapter.HANDLERS) == {"status", "notify_test", "receive_test"}
    assert {f["name"] for f in hub_adapter.API_MANIFEST["functions"]} == set(hub_adapter.HANDLERS)


async def test_a_failed_release_locks_pages_and_refuses(svc, alerts):
    _, token = await svc.acquire("fir")
    await svc.release("fir", token, ok=False, reason="master never came up")
    assert svc._read_lock(FIR) is not None
    assert len(alerts) == 1 and "/approve" in alerts[0] and "master never came up" in alerts[0]
    assert (await svc.acquire("fir"))[0] == "locked"


async def test_a_good_release_frees_the_slot(svc, alerts):
    _, token = await svc.acquire("fir")
    await svc.release("fir", token, ok=True)
    assert svc._read_lock(FIR) is None and alerts == []
    assert (await svc.acquire("fir"))[0] == "granted"


async def test_a_stale_release_is_ignored(svc, alerts):
    await svc.acquire("fir")
    await svc.release("fir", "not-the-token", ok=False, reason="late")
    assert svc._read_lock(FIR) is None and "fir" in svc._leased


async def test_an_approve_grants_exactly_one_attempt(svc, alerts):
    _, token = await svc.acquire("fir")
    await svc.release("fir", token, ok=False, reason="boom")
    svc._handle_approve({"command": "approve", "device": FIR.twofa_device})
    status, token2 = await svc.acquire("fir")
    assert status == "granted" and svc._read_lock(FIR) is None
    await svc.release("fir", token2, ok=False, reason="boom2")
    assert (await svc.acquire("fir"))[0] == "locked"


def test_approve_matches_only_its_device_and_command(svc):
    svc._handle_approve({"command": "deny", "device": "alliance"})
    svc._handle_approve({"command": "approve"})
    assert not svc._approve_active("alliance")
    svc._handle_approve({"command": "approve", "device": "alliance"})
    assert svc._approve_active("alliance") and not svc._approve_active("cwl")
    assert "not a device" in svc._handle_approve({"command": "approve", "device": "typo"})
    assert not svc._approve_active("typo")


async def test_a_failed_receipt_leaves_the_window_open(svc):
    await svc._on_approve_event({"command": "approve", "device": FIR.twofa_device,
                                 "account": "discord-bot", "channel_id": "123"})
    assert svc._approve_active(FIR.twofa_device)


@pytest.mark.parametrize("frames, why", [
    (['{"verdict":"fail","reason":"master never came up"}'], "master never came up"),
    ([], "without a verdict"),
])
async def test_a_failed_or_dropped_lease_locks_and_names_the_requester(svc, alerts, frames, why):
    bridge = _FakeBridge(frames)
    await svc.lease_session(_FakeCtx("fir", bridge, node="capella"))
    assert json.loads(bridge.sent[0]) == {"lease": "granted"}
    assert svc._read_lock(FIR) is not None and "fir" not in svc._leased
    assert len(alerts) == 1 and why in alerts[0] and "capella" in alerts[0]


async def test_a_second_lease_is_busy(svc):
    await svc.acquire("fir")
    bridge = _FakeBridge([])
    await svc.lease_session(_FakeCtx("fir", bridge))
    assert json.loads(bridge.sent[0])["lease"] == "busy" and bridge.closed


async def test_a_timeout_with_a_dead_approver_is_ours(svc, monkeypatch):
    _answer_2fa(monkeypatch, ping={"reachable": False})
    assert await svc._classify_cause(FIR, "connect exceeded 120s") == arbiter._CAUSE_SELF


async def test_an_auth_rejection_is_never_ours(svc, monkeypatch):
    _answer_2fa(monkeypatch, ping={"reachable": False})
    reason = "connect exceeded 120s; ssh: Permission denied (keyboard-interactive)."
    assert await svc._classify_cause(FIR, reason) == arbiter._CAUSE_EXTERNAL


@pytest.mark.parametrize("cause, ping, cleared", [
    (arbiter._CAUSE_EXTERNAL, {"reachable": True}, False),
    (arbiter._CAUSE_SELF, {"reachable": False}, False),
    (arbiter._CAUSE_SELF, {"reachable": True}, True),
])
async def test_a_hold_lifts_only_on_a_positive_assertion(svc, alerts, monkeypatch, cause, ping, cleared):
    _answer_2fa(monkeypatch, ping=ping)
    svc._write_lock(FIR, "connect exceeded 120s", cause=cause)
    assert await svc._maybe_self_clear(FIR) is cleared
    assert (svc._read_lock(FIR) is None) is cleared


async def test_an_unreadable_lockfile_still_holds(svc, monkeypatch):
    _answer_2fa(monkeypatch, ping={"reachable": True})
    Path(lock_path(FIR)).write_text("{not json")
    assert await svc._maybe_self_clear(FIR) is False
    assert (await svc.acquire("fir"))[0] == "locked"


def test_lock_write_leaves_only_the_lockfile(svc, locks):
    svc._write_lock(FIR, "reason", cause=arbiter._CAUSE_SELF)
    assert [p.name for p in locks.iterdir()] == ["fir.lock"]
    assert json.loads(Path(lock_path(FIR)).read_text())["cause"] == arbiter._CAUSE_SELF
