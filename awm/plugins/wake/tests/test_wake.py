from __future__ import annotations

import socket

import pytest

from awm.wake import config, magic

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


def test_bad_mac_is_refused():
    with pytest.raises(config.ConfigError, match="not a MAC"):
        config.parse({"node": "m", "targets": {"x": {"mac": "nope", "broadcast": "b",
                                                     "probe_host": "h"}}})


def test_partner_needs_a_target_entry():
    with pytest.raises(config.ConfigError, match="partner"):
        config.parse({"node": "m", "standby": {"partner": "alcor"}})


def test_missing_file_names_the_role(tmp_path):
    with pytest.raises(config.ConfigError, match="wake_node"):
        config.load(tmp_path / "wake.toml")


class FakeSock:
    def __init__(self):
        self.opts, self.sent = [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def setsockopt(self, *a):
        self.opts.append(a)

    def sendto(self, data, addr):
        self.sent.append((data, addr))


def test_send_broadcasts_to_subnet_and_limited(cfg):
    sock = FakeSock()
    sent = magic.send(cfg.target("capella"), sock_factory=lambda: sock)
    assert sent == ["172.16.0.255", "255.255.255.255"]
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in sock.opts
    assert all(addr[1] == 9 for _, addr in sock.sent)


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


async def test_wake_times_out_with_a_hint(cfg):
    clock = Clock()

    async def probe(h, p):
        return False

    out = await magic.wake(cfg.target("alcor"), probe=probe, sender=lambda t: ["b"],
                           budget_s=60, clock=clock, sleep=clock.sleep, poll_s=10)
    assert out["status"] == "timeout" and "BIOS" in out["hint"]


async def test_answers_sees_a_listening_port():
    import asyncio

    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert await magic.answers("127.0.0.1", port)
    server.close()
    await server.wait_closed()
    assert not await magic.answers("127.0.0.1", port, timeout=0.5)
