"""Unit tests for the hook→wake funnel: classification, per-game debounce, and
the agent.wake emission shape. No sockets — events are fed to ``_handle``
directly against a recording stub adapter."""

from __future__ import annotations

import asyncio

import pytest

from awm.events.funnel import Funnel, WakeSource, WAKE_TOPIC


class StubAdapter:
    def __init__(self):
        self.emits: list[tuple[str, dict]] = []

    async def emit(self, topic: str, payload: dict) -> None:
        self.emits.append((topic, payload))


@pytest.fixture()
def src():
    return WakeSource(service="rlm-factorio", topic="factorio", game="factorio")


@pytest.fixture()
def funnel(src):
    return Funnel(StubAdapter(), sources=[src])


def _run(coro):
    return asyncio.run(coro)


class TestClassify:
    def test_wake_worthy_kinds(self, funnel, src):
        assert funnel._classify(src, {"kind": "died"}) == "died"
        assert funnel._classify(src, {"kind": "error"}) == "error"

    def test_non_wake_kinds_dropped(self, funnel, src):
        assert funnel._classify(src, {"kind": "world_saved"}) is None
        assert funnel._classify(src, {"kind": "arrived"}) is None
        assert funnel._classify(src, {"kind": ""}) is None
        assert funnel._classify(src, "not a dict") is None
        assert funnel._classify(src, {}) is None


class TestDebounce:
    def test_first_wake_passes_then_debounced(self, funnel):
        assert funnel._debounced("factorio", now=100.0) is True
        assert funnel._debounced("factorio", now=100.0 + 1.0) is False
        assert funnel._debounced("factorio", now=100.0 + 59.9) is False

    def test_wake_passes_after_window(self, funnel):
        assert funnel._debounced("factorio", now=100.0) is True
        assert funnel._debounced("factorio", now=100.0 + 60.5) is True

    def test_debounce_is_per_game(self, funnel):
        assert funnel._debounced("factorio", now=100.0) is True
        assert funnel._debounced("othergame", now=100.0) is True


class TestHandle:
    def test_emits_normalized_wake(self, funnel, src):
        handled = _run(funnel._handle(src, {"kind": "died", "session_id": "s1"}))
        assert handled is True
        assert funnel.adapter.emits == [(WAKE_TOPIC, {
            "game": "factorio", "reason": "died",
            "source": "rlm-factorio/factorio",
        })]

    def test_debounced_second_event_not_emitted(self, funnel, src):
        assert _run(funnel._handle(src, {"kind": "died"})) is True
        assert _run(funnel._handle(src, {"kind": "error"})) is False
        assert len(funnel.adapter.emits) == 1

    def test_non_wake_event_not_emitted(self, funnel, src):
        assert _run(funnel._handle(src, {"kind": "world_saved"})) is False
        assert funnel.adapter.emits == []


class _StopLoop(Exception):
    pass


class TestListen:
    """``_listen`` must not knock on a disabled source's emitter: each refused
    subscribe is two gateway log lines, forever."""

    def _drive(self, monkeypatch, funnel, src, *, enabled: list[bool]):
        from awm import gatewayclient
        import awm.events.funnel as funnel_mod

        subscribes: list[str] = []
        sleeps: list[float] = []
        answers = iter(enabled)

        async def fake_enabled(service):
            return next(answers)

        async def fake_subscribe(service, topic):
            subscribes.append(service)
            raise ConnectionError("403")
            yield  # pragma: no cover — makes this an async generator

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= len(enabled):
                raise _StopLoop

        monkeypatch.setattr(funnel, "_source_enabled", fake_enabled)
        monkeypatch.setattr(gatewayclient, "subscribe", fake_subscribe)
        monkeypatch.setattr(funnel_mod.asyncio, "sleep", fake_sleep)
        with pytest.raises(_StopLoop):
            _run(funnel._listen(src))
        return subscribes, sleeps

    def test_disabled_source_is_never_subscribed(self, monkeypatch, funnel, src):
        from awm.events.funnel import DISABLED_RECHECK_S
        subs, sleeps = self._drive(monkeypatch, funnel, src,
                                   enabled=[False, False, False])
        assert subs == []
        assert sleeps == [DISABLED_RECHECK_S] * 3

    def test_source_enabled_later_is_subscribed(self, monkeypatch, funnel, src):
        subs, _ = self._drive(monkeypatch, funnel, src,
                              enabled=[False, True, True])
        assert subs == ["rlm-factorio", "rlm-factorio"]

    def test_probe_doubt_answers_enabled(self, monkeypatch, funnel):
        import httpx

        class Boom:
            def __init__(self, *a, **k):
                raise httpx.ConnectError("gateway down")

        monkeypatch.setattr(httpx, "AsyncClient", Boom)
        assert _run(funnel._source_enabled("rlm-factorio")) is True
