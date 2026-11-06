"""Sync now orders that alcor's approver delivers through deneb's door (fleet.md R83, R79)."""

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

    def order(self, order_id="o1", node="deneb", kind="zotero_sync", ttl=600, key=None):
        body = {"order_id": order_id, "kind": kind, "node": node, "payload": {},
                "not_after": int(time.time()) + ttl}
        key = key or self.key
        env = {"order": body, "sig": verify_grant.b64u(key.sign(verify_grant.canon(body))),
               "key_id": verify_grant.key_id(self.key.public_key())}
        return env


@pytest.fixture
def alcor():
    return Alcor()


@pytest.fixture
def make(alcor, tmp_path):
    def make():
        started = []

        async def start_sync(args):
            started.append(args)

        return orders.Orders(orders.grant_verifier(alcor.pubkey, "deneb"),
                             orders.Seen(tmp_path / "seen.json"), start_sync), started
    return make


def test_a_signed_order_starts_one_sync(alcor, make):
    o, started = make()
    out = asyncio.run(o.take(alcor.order()))
    assert out == {"status": "started", "order_id": "o1"}
    assert started == [{"trigger": "order", "force": False}]


def test_a_replayed_order_does_nothing(alcor, make):
    o, started = make()
    env = alcor.order()
    asyncio.run(o.take(env))
    assert asyncio.run(o.take(env))["status"] == "replay"
    assert len(started) == 1


def test_seen_survives_a_restart(alcor, make):
    o, _ = make()
    env = alcor.order()
    asyncio.run(o.take(env))
    o2, started2 = make()
    assert asyncio.run(o2.take(env))["status"] == "replay" and started2 == []


@pytest.mark.parametrize("bad", [
    {"node": "altair"},
    {"kind": "trilium_wipe"},
    {"ttl": -5},
    {"key": "forged"},
])
def test_an_order_that_does_not_verify_is_refused(alcor, make, bad):
    if bad.get("key") == "forged":
        bad = {"key": ed25519.Ed25519PrivateKey.generate()}
    o, started = make()
    out = asyncio.run(o.take(alcor.order(**bad)))
    assert out["status"] == "refused" and started == []


def test_off_until_configured():
    assert orders.Settings.from_env({}) is None
    s = orders.Settings.from_env({"ZOTERO_ORDERS_PUBKEY": "k",
                                  "AWM_NODE_NAME": "deneb", "AWM_DIR": "/x"})
    assert s.node == "deneb" and str(s.seen_path) == "/x/zotero/orders-seen.json"
