"""Sync now orders from alcor's approver (fleet.md R83): verify, run once, report."""

from __future__ import annotations

import asyncio
import time

import pytest

from awm.zotero import orders

verify_grant = pytest.importorskip("verify_grant")
ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class Alcor:
    """Signs orders as alcor's grant key does, and records what the node reports."""

    def __init__(self):
        self.key = ed25519.Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.pubkey = verify_grant.b64u(raw)
        self.queue: list[dict] = []
        self.done: list[dict] = []

    def order(self, order_id="o1", node="deneb", kind="zotero_sync", ttl=600, key=None):
        body = {"order_id": order_id, "kind": kind, "node": node, "payload": {},
                "not_after": int(time.time()) + ttl}
        key = key or self.key
        env = {"order": body, "sig": verify_grant.b64u(key.sign(verify_grant.canon(body))),
               "key_id": verify_grant.key_id(self.key.public_key())}
        self.queue.append(env)
        return env

    async def call(self, verb, args):
        if verb == "order_pull":
            out, self.queue = self.queue, []
            return out
        self.done.append(args)
        return {"recorded": True}


@pytest.fixture
def alcor():
    return Alcor()


@pytest.fixture
def make(alcor, tmp_path):
    def make(run_sync=None):
        ran = []

        async def default_sync(args):
            ran.append(args)
            return {"status": "ok", "items": 3}

        p = orders.Puller("deneb", alcor.call, orders.grant_verifier(alcor.pubkey, "deneb"),
                          orders.Seen(tmp_path / "seen.json"), run_sync or default_sync)
        return p, ran
    return make


def test_a_signed_order_runs_one_sync_and_reports(alcor, make):
    p, ran = make()
    alcor.order()
    out = asyncio.run(p.tick(wait_s=0))
    assert out[0]["status"] == "ran" and out[0]["ok"]
    assert ran == [{"trigger": "order", "force": False}]
    assert alcor.done == [{"order_id": "o1", "ok": True, "note": '{"status": "ok", "items": 3}'}]


def test_a_replayed_order_does_nothing(alcor, make):
    p, ran = make()
    env = alcor.order()
    asyncio.run(p.tick(wait_s=0))
    alcor.queue.append(env)
    assert asyncio.run(p.tick(wait_s=0))[0]["status"] == "replay"
    assert len(ran) == 1


def test_seen_survives_a_restart(alcor, make):
    p, ran = make()
    env = alcor.order()
    asyncio.run(p.tick(wait_s=0))
    p2, ran2 = make()
    alcor.queue.append(env)
    assert asyncio.run(p2.tick(wait_s=0))[0]["status"] == "replay" and ran2 == []


@pytest.mark.parametrize("bad", [
    {"node": "altair"},
    {"kind": "trilium_wipe"},
    {"ttl": -5},
    {"key": "forged"},
])
def test_an_order_that_does_not_verify_is_dropped(alcor, make, bad):
    if bad.get("key") == "forged":
        bad = {"key": ed25519.Ed25519PrivateKey.generate()}
    p, ran = make()
    alcor.order(**bad)
    out = asyncio.run(p.tick(wait_s=0))
    assert out[0]["status"] == "refused" and ran == [] and alcor.done == []


def test_a_failed_sync_reports_not_ok(alcor, make):
    async def boom(args):
        raise RuntimeError("library unreachable")

    p, _ = make(boom)
    alcor.order()
    out = asyncio.run(p.tick(wait_s=0))
    assert out[0]["ok"] is False and "library unreachable" in alcor.done[0]["note"]


def test_off_until_configured():
    assert orders.Settings.from_env({}) is None
    assert orders.Settings.from_env({"ZOTERO_ORDERS_APPROVER": "alcor"}) is None
    s = orders.Settings.from_env({"ZOTERO_ORDERS_APPROVER": "alcor", "ZOTERO_ORDERS_PUBKEY": "k",
                                  "AWM_NODE_NAME": "deneb", "AWM_DIR": "/x"})
    assert s.node == "deneb" and str(s.seen_path) == "/x/zotero/orders-seen.json"
