from __future__ import annotations

import pytest

from awm.wake import magic

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_packet_is_six_ff_then_sixteen_macs():
    p = magic.packet("08:bf:b8:6b:b2:fc")
    assert len(p) == 102
    assert p[:6] == b"\xff" * 6
    assert p[6:12] == bytes.fromhex("08bfb86bb2fc")
    assert p[6:] == bytes.fromhex("08bfb86bb2fc") * 16


def test_r139_matrix(cfg):
    assert cfg.refusal("alcor") is None
    assert cfg.refusal("capella") is None
    assert "may not wake mira" in cfg.refusal("mira")
    assert "cannot wake itself" in cfg.refusal("mizar")
    assert "no wake entry" in cfg.refusal("altair")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


async def test_wake_reports_seconds_to_ssh(cfg):
    clock = Clock()
    answers = iter([False, False, False, True])

    async def probe(h, p):
        return next(answers)

    out = await magic.wake(cfg.target("capella"), probe=probe, sender=lambda t: ["b"],
                           clock=clock, sleep=clock.sleep, poll_s=30)
    assert out["status"] == "up" and out["seconds"] == 90


async def test_wake_already_up_sends_nothing(cfg):
    sent = []

    async def probe(h, p):
        return True

    out = await magic.wake(cfg.target("alcor"), probe=probe, sender=sent.append)
    assert out["status"] == "already_up" and sent == []
