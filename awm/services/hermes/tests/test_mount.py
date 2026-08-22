"""The gateway mount's contract with the dashboard.

The mount is the whole reason this service can put the GUI behind awm's edge
session instead of on a dedicated TLS front, and it rests on one header. If the
registration ever loses ``strip_prefix``, every request under the prefix 404s
with nothing in the browser to explain it.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.hermes import mount

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Client:
    """Captures the one registration POST, then stops the loop."""

    def __init__(self, sink, body):
        self._sink = sink
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        self._sink.append((url, json))
        raise _Stop()


class _Stop(Exception):
    pass


def _run_one_registration(monkeypatch, body):
    sink: list[tuple] = []
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:12100")
    monkeypatch.setattr(mount.httpx, "AsyncClient",
                        lambda **k: _Client(sink, body))

    async def _once():
        # `hold` retries forever by design; run it exactly long enough to make
        # the first registration attempt.
        task = asyncio.create_task(mount.hold())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if sink:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_once())
    return sink


def test_registration_asks_for_prefix_stripping(monkeypatch):
    sink = _run_one_registration(monkeypatch, {"strip_prefix": True})
    assert sink, "no registration was attempted"
    url, payload = sink[0]
    assert url.endswith("/hub/register")
    assert payload["prefix"] == mount.PREFIX
    assert payload["strip_prefix"] is True
    assert payload["url"].startswith("http://127.0.0.1:")


def test_public_url_is_the_edge_plus_the_prefix(monkeypatch):
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.0.0.1:12100/")
    assert mount.public_url() == f"https://10.0.0.1:12100{mount.PREFIX}/"


def test_public_url_degrades_to_the_bare_prefix_without_an_edge(monkeypatch):
    monkeypatch.delenv("AWM_EDGE_URL", raising=False)
    assert mount.public_url() == f"{mount.PREFIX}/"
