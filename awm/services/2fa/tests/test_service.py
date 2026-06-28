"""Service-runtime tests — verb wiring + the background burst task — with a
fake engine, so nothing touches Duo or the network."""

from __future__ import annotations

import asyncio
import types

import pytest

from awm.twofa.config import Config
from awm.twofa.service import TwoFAService


def unenrolled_cfg(tmp_path) -> Config:
    return Config(creds_path=tmp_path / "creds.json", key_path=tmp_path / "key.pem")


class FakeEngine:
    def __init__(self) -> None:
        self.approved_count = 0
        self.calls: list[tuple] = []
        self.client = types.SimpleNamespace(
            host="api-test.duosecurity.com",
            get_transactions=lambda: [],
        )

    def held_transactions(self):
        return []

    def approve_all_remaining(self):
        return 0.0

    def approve(self, urgid, by="awm"):
        self.calls.append(("approve", urgid))
        return True

    def deny(self, urgid, by="awm"):
        self.calls.append(("deny", urgid))
        return True

    def approve_all(self, by="awm"):
        self.calls.append(("approve_all",))
        return 3

    def handle_transactions(self, txs):
        self.calls.append(("handle", len(txs)))


def inject(svc: TwoFAService, eng: FakeEngine) -> None:
    """Pre-seed the cached engine so _load_engine returns it without reading
    creds off disk."""
    svc._engine = eng
    svc._client = eng.client


@pytest.mark.smoke
def test_unenrolled_reports_cleanly(tmp_path):
    svc = TwoFAService(unenrolled_cfg(tmp_path))
    svc.init()  # must not raise without creds
    assert svc.ping()["enrolled"] is False
    st = svc.status()
    assert st["enrolled"] is False
    assert st["held"] == []
    assert svc.pending()["held"] == []


@pytest.mark.smoke
def test_approve_deny_approve_all_delegate(tmp_path):
    svc = TwoFAService(unenrolled_cfg(tmp_path))
    eng = FakeEngine()
    inject(svc, eng)
    assert svc.approve("u1") == {"ok": True, "urgid": "u1"}
    assert svc.deny("u2") == {"ok": True, "urgid": "u2"}
    assert svc.approve_all()["cleared"] == 3
    assert ("approve", "u1") in eng.calls
    assert ("deny", "u2") in eng.calls
    assert ("approve_all",) in eng.calls


@pytest.mark.smoke
def test_status_with_engine_reports_host(tmp_path):
    # Touch the cred files so cfg.enrolled is True; the injected engine means
    # status() never actually reads them.
    cfg = unenrolled_cfg(tmp_path)
    cfg.creds_path.write_text("{}")
    cfg.key_path.write_text("x")
    svc = TwoFAService(cfg)
    inject(svc, FakeEngine())
    st = svc.status()
    assert st["enrolled"] is True
    assert st["host"] == "api-test.duosecurity.com"
    assert st["burst_active"] is False


@pytest.mark.smoke
async def test_burst_starts_then_already_running(tmp_path):
    cfg = Config(
        creds_path=tmp_path / "creds.json", key_path=tmp_path / "key.pem",
        burst_window_seconds=0.3, burst_interval_seconds=0.05,
    )
    svc = TwoFAService(cfg)
    inject(svc, FakeEngine())

    r1 = await svc.start_burst()
    assert r1["status"] == "started"
    assert svc._burst_active() is True

    r2 = await svc.start_burst()
    assert r2["status"] == "already-running"

    await asyncio.sleep(0.4)
    assert svc._burst_active() is False
    assert svc._last_burst is not None
