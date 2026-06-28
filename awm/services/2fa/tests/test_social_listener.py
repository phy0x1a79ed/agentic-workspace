"""Tests for the social slash-command handler — the bridge from a Discord
``/approve`` (emitted by the social service) to a Duo approval burst. The burst
itself and the reply hop are stubbed, so nothing touches Duo or the gateway."""

from __future__ import annotations

import pytest

from awm.twofa.config import Config, DeviceCreds
from awm.twofa.service import TwoFAService

pytestmark = [pytest.mark.smoke]


def _creds(tmp_path, name):
    return DeviceCreds(name, tmp_path / f"{name}-creds.json", tmp_path / f"{name}-key.pem")


def _enrolled_cfg(tmp_path, *names):
    """Config whose named devices are all enrolled (cred files touched)."""
    names = names or ("cwl",)
    devices = {n: _creds(tmp_path, n) for n in names}
    for d in devices.values():
        d.creds_path.write_text("{}")
        d.key_path.write_text("x")
    return Config(devices=devices)


def _instrument(svc):
    """Replace start_burst + _social_reply with recorders."""
    bursts, replies = [], []

    async def _rec_burst(device, window=None, interval=None, count=1):
        bursts.append({"device": device, "window": window,
                       "interval": interval, "count": count})
        return {"status": "started", "device": device}

    async def _rec_reply(ev, text):
        replies.append(text)

    svc.start_burst = _rec_burst       # type: ignore[assignment]
    svc._social_reply = _rec_reply     # type: ignore[assignment]
    return bursts, replies


async def test_approve_arms_burst_with_social_tunables(tmp_path):
    svc = TwoFAService(_enrolled_cfg(tmp_path, "cwl"))
    bursts, replies = _instrument(svc)

    await svc._handle_social_command(
        {"command": "approve", "device": "cwl", "account": "bot", "channel_id": "9"})

    assert len(bursts) == 1
    b = bursts[0]
    assert b["device"] == "cwl"
    assert b["window"] == 300.0      # 5 minutes
    assert b["interval"] == 2.0      # 2-second polls
    assert b["count"] == 1000        # stays armed the whole window
    assert replies and replies[0].startswith("✅")


async def test_missing_device_uses_default(tmp_path):
    svc = TwoFAService(_enrolled_cfg(tmp_path, "cwl"))
    bursts, _ = _instrument(svc)

    await svc._handle_social_command({"command": "approve"})  # no device

    assert bursts[0]["device"] == "cwl"  # social_default_device


async def test_explicit_device_routes(tmp_path):
    svc = TwoFAService(_enrolled_cfg(tmp_path, "cwl", "alliance"))
    bursts, _ = _instrument(svc)

    await svc._handle_social_command({"command": "approve", "device": "alliance"})

    assert bursts[0]["device"] == "alliance"


async def test_unenrolled_device_no_burst_but_warns(tmp_path):
    # cwl enrolled, alliance declared-but-not-enrolled.
    cfg = _enrolled_cfg(tmp_path, "cwl")
    cfg.devices["alliance"] = _creds(tmp_path, "alliance")  # no cred files
    svc = TwoFAService(cfg)
    bursts, replies = _instrument(svc)

    await svc._handle_social_command({"command": "approve", "device": "alliance"})

    assert bursts == []
    assert replies and replies[0].startswith("⚠️")


async def test_non_approve_and_garbage_ignored(tmp_path):
    svc = TwoFAService(_enrolled_cfg(tmp_path, "cwl"))
    bursts, replies = _instrument(svc)

    await svc._handle_social_command({"command": "ping"})
    await svc._handle_social_command("not a dict")
    await svc._handle_social_command({})

    assert bursts == [] and replies == []


def test_subscription_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_2FA_SOCIAL_SUBSCRIBE", "0")
    # Fresh Config reads the env at construction.
    cfg = Config(devices={"cwl": _creds(tmp_path, "cwl")})
    assert cfg.social_subscribe is False
    svc = TwoFAService(cfg)
    svc.init()  # must not spawn a listener nor raise
    assert svc._social_task is None
