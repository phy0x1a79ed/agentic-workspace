"""Service-side give-up tests for ``ServiceAdapter`` (lifecycle plan T2).

The adapter's run loop must exit cleanly (``return`` → process exits 0, no
retry) on three stand-down signals — a 409 register reject, a terminal control
WS close (4409 lease-held / 4404 unknown), and an in-band ``shutdown`` frame —
and must also give up after the reconnect deadline once the gateway is gone for
good. A bare transient blip must keep retrying.

These drive the real ``_register`` / ``_serve`` / ``run`` against fakes (no
gateway, no sockets), so they stay sync (the dist carries no pytest-asyncio
config — async work is driven via ``asyncio.run``).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close

from awm.gatewayclient import adapter as adapter_mod
from awm.gatewayclient.adapter import GiveUp, ServiceAdapter


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWS:
    """Minimal async-iterable WS. Yields ``frames`` then either raises
    ``raise_on_end`` (e.g. a ConnectionClosed) or stops iteration."""

    def __init__(self, frames=None, raise_on_end=None):
        self._frames = list(frames or [])
        self._raise_on_end = raise_on_end
        self.sent: list[str] = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        if self._raise_on_end is not None:
            raise self._raise_on_end
        raise StopAsyncIteration


class FakeConnect:
    """Async context manager standing in for ``websockets.connect(...)``.
    If ``raise_on_enter`` is set, raises it on ``__aenter__`` (upgrade fail)."""

    def __init__(self, ws=None, raise_on_enter=None):
        self._ws = ws
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self._ws

    async def __aexit__(self, *exc):
        return False


class FakeResp:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )


class FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        return self._resp


def _adapter() -> ServiceAdapter:
    return ServiceAdapter("svc-x", {"functions": []}, {})


def _register(a: ServiceAdapter, hub_url: str):
    """Drive ``_register`` with the explicit base params ``run`` now passes."""
    return a._register(hub_url, name=a.name, prefix=f"/svc/{a.name}", overlay=False)


# ---------------------------------------------------------------------------
# _register: 409 → GiveUp; 2xx → service_id; other 4xx → retryable
# ---------------------------------------------------------------------------


def test_register_409_raises_giveup(monkeypatch):
    monkeypatch.setattr(adapter_mod.httpx, "AsyncClient",
                        lambda *a, **k: FakeClient(FakeResp(409, text="dup")))
    with pytest.raises(GiveUp):
        asyncio.run(_register(_adapter(), "http://hub"))


def test_register_2xx_returns_service_id(monkeypatch):
    monkeypatch.setattr(
        adapter_mod.httpx, "AsyncClient",
        lambda *a, **k: FakeClient(FakeResp(200, payload={"service_id": "sid-7"})))
    sid = asyncio.run(_register(_adapter(), "http://hub"))
    assert sid == "sid-7"


def test_register_500_is_retryable_not_giveup(monkeypatch):
    monkeypatch.setattr(adapter_mod.httpx, "AsyncClient",
                        lambda *a, **k: FakeClient(FakeResp(500)))
    # Not a GiveUp — raise_for_status surfaces a transient HTTP error instead.
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_register(_adapter(), "http://hub"))


# ---------------------------------------------------------------------------
# _serve: terminal closes → GiveUp; transient close → re-raise; shutdown frame
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [4409, 4404])
def test_serve_terminal_close_raises_giveup(monkeypatch, code):
    ws = FakeWS(raise_on_end=ConnectionClosed(Close(code, ""), None))
    monkeypatch.setattr(adapter_mod.websockets, "connect",
                        lambda *a, **k: FakeConnect(ws=ws))
    with pytest.raises(GiveUp):
        asyncio.run(_adapter()._serve("http://hub", "sid"))


def test_serve_transient_close_reraises(monkeypatch):
    # 1006 abnormal closure is transient — _serve must NOT swallow it as GiveUp.
    ws = FakeWS(raise_on_end=ConnectionClosed(Close(1006, ""), None))
    monkeypatch.setattr(adapter_mod.websockets, "connect",
                        lambda *a, **k: FakeConnect(ws=ws))
    with pytest.raises(ConnectionClosed):
        asyncio.run(_adapter()._serve("http://hub", "sid"))


def test_serve_upgrade_409_raises_giveup(monkeypatch):
    resp = httpx.Response(409)
    monkeypatch.setattr(
        adapter_mod.websockets, "connect",
        lambda *a, **k: FakeConnect(raise_on_enter=InvalidStatus(resp)))
    with pytest.raises(GiveUp):
        asyncio.run(_adapter()._serve("http://hub", "sid"))


def test_serve_shutdown_frame_raises_giveup(monkeypatch):
    ws = FakeWS(frames=['{"kind": "shutdown"}'])
    monkeypatch.setattr(adapter_mod.websockets, "connect",
                        lambda *a, **k: FakeConnect(ws=ws))
    with pytest.raises(GiveUp):
        asyncio.run(_adapter()._serve("http://hub", "sid"))


# ---------------------------------------------------------------------------
# run(): returns on GiveUp; returns after the reconnect deadline
# ---------------------------------------------------------------------------


def test_run_returns_on_giveup(monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://hub")
    monkeypatch.setenv("AWM_SERVICE_ID", "sid-preset")  # skip _register

    a = _adapter()

    async def _serve_giveup(hub_url, sid, **kw):
        raise GiveUp("stand down")

    monkeypatch.setattr(a, "_serve", _serve_giveup)
    # Should return promptly, not loop.
    asyncio.run(asyncio.wait_for(a.run(), timeout=2))


def test_run_returns_after_deadline_of_transient_failures(monkeypatch):
    monkeypatch.setenv("AWM_HUB_URL", "http://hub")
    monkeypatch.setenv("AWM_SERVICE_ID", "sid-preset")
    monkeypatch.setattr(adapter_mod, "_RECONNECT_DEADLINE_S", 10.0)

    # Fake clock: 0 (init), 6 (1st check, < 10 → retry), 12 (2nd check, > 10 →
    # give up). Demonstrates the deadline counts repeated transient failures.
    ticks = iter([0.0, 6.0, 12.0, 99.0, 99.0])
    monkeypatch.setattr(adapter_mod, "monotonic", lambda: next(ticks))

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", _no_sleep)

    a = _adapter()
    calls = {"n": 0}

    async def _serve_transient(hub_url, sid, **kw):
        calls["n"] += 1
        raise RuntimeError("blip")

    monkeypatch.setattr(a, "_serve", _serve_transient)
    asyncio.run(asyncio.wait_for(a.run(), timeout=2))
    # Retried at least once before the deadline tripped.
    assert calls["n"] >= 2
