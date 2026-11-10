from __future__ import annotations

import pytest

from awm.wake import config
from awm.wake.gateway import REFLECTION, WAKE, CallError

TOML = {
    "node": "mizar",
    "standby": {"partner": "alcor", "deadline_s": 300, "rewake_every_s": 120, "rewake_max": 2,
                "reboot_command": ["true"]},
    "targets": {
        "alcor": {"mac": "00:23:24:d8:15:c9", "broadcast": "172.16.0.255",
                  "probe_host": "172.16.0.23", "wakers": ["mizar"]},
        "capella": {"mac": "08:bf:b8:6b:b2:fc", "broadcast": "172.16.0.255",
                    "probe_host": "172.16.0.21", "wakers": ["mizar", "alcor"]},
        "mira": {"mac": "aa:bb:cc:dd:ee:ff", "broadcast": "172.16.0.255",
                 "probe_host": "172.16.0.24", "wakers": []},
    },
}


@pytest.fixture()
def cfg():
    return config.parse(TOML)


class FakeGateway:
    """Records calls and answers them from a table of (tool, verb) -> reply or exception."""

    def __init__(self, replies=None, up=True, harness=None):
        self.calls = []
        self.replies = replies or {}
        self._up = up
        self._harness = harness

    async def harness(self):
        return self._harness

    async def call(self, tool, verb, args=None, peer=None):
        self.calls.append((tool, verb, args or {}, peer))
        reply = self.replies.get((tool, verb), {"ok": True})
        if isinstance(reply, Exception):
            raise reply
        return reply(args) if callable(reply) else reply

    async def up(self):
        return self._up(len(self.calls)) if callable(self._up) else self._up

    def verbs(self):
        return [v for _, v, _, _ in self.calls]


@pytest.fixture()
def gw():
    return FakeGateway({(REFLECTION, "start_other"): {"session_id": "cc" + "a" * 32, "link": "https://x/s"}})


__all__ = ["FakeGateway", "REFLECTION", "WAKE", "CallError"]
