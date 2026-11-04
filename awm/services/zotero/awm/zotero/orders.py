"""Run a sync when Tony presses Sync now on the bibliography page (fleet.md R83).

The button lives on alcor's approver panel. A press becomes an order envelope
signed with alcor's grant key. This node long-polls the approver for its
orders, checks each signature with alcor's public key, runs each order once,
and reports back. Nothing here can be triggered from deneb's public front: an
order that does not verify is dropped.

The puller is off until the config names the approver's node and alcor's
public key (``ZOTERO_ORDERS_APPROVER`` and ``ZOTERO_ORDERS_PUBKEY``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("awm.zotero.orders")

KIND = "zotero_sync"
WAIT_S = 55

Call = Callable[[str, dict], Awaitable[Any]]
Verify = Callable[[dict], dict]


class OrderRefused(Exception):
    """An envelope this node will not run."""


@dataclass(frozen=True)
class Settings:
    approver: str
    pubkey: str
    node: str
    seen_path: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings | None:
        env = os.environ if env is None else env
        approver = env.get("ZOTERO_ORDERS_APPROVER", "").strip()
        pubkey = env.get("ZOTERO_ORDERS_PUBKEY", "").strip()
        if not approver or not pubkey:
            return None
        node = env.get("AWM_NODE_NAME") or socket.gethostname().split(".")[0]
        awm_dir = Path(env.get("AWM_DIR") or Path.home() / "agentic_workspace" / ".awm")
        return cls(approver, pubkey, node, awm_dir / "zotero" / "orders-seen.json")


def grant_verifier(pubkey: str, node: str) -> Verify:
    """Check an envelope with the approver's grantlib (``verify_grant``)."""
    import verify_grant

    pub = verify_grant.load_public_key(pubkey)

    def verify(envelope: dict) -> dict:
        try:
            return verify_grant.verify_order(envelope, pub, node=node)
        except verify_grant.GrantInvalid as exc:
            raise OrderRefused(str(exc)) from None

    return verify


def approver_call(peer: str) -> Call:
    """Reach the approver on its node, over the edge."""
    from awm import gatewayclient

    async def call(verb: str, args: dict) -> Any:
        return await gatewayclient.call("approver", verb, args, peer=peer, timeout=WAIT_S + 20)

    return call


class Seen:
    """The order ids already run, kept until each order's own expiry."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = path, clock
        try:
            self.ids: dict[str, int] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.ids = {}

    def __contains__(self, order_id: str) -> bool:
        return order_id in self.ids

    def add(self, order_id: str, not_after: int) -> None:
        now = self.clock()
        self.ids = {k: v for k, v in self.ids.items() if v > now}
        self.ids[order_id] = int(not_after)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ids))
        tmp.replace(self.path)


class Puller:
    def __init__(self, node: str, call: Call, verify: Verify, seen: Seen,
                 run_sync: Callable[[dict], Awaitable[dict]]):
        self.node, self.call, self.verify, self.seen = node, call, verify, seen
        self.run_sync = run_sync

    async def tick(self, wait_s: float = WAIT_S) -> list[dict]:
        """Pull once and run what verifies. Returns one outcome per envelope."""
        envelopes = await self.call("order_pull", {"node": self.node, "kinds": [KIND],
                                                   "wait_s": wait_s}) or []
        return [await self._one(env) for env in envelopes]

    async def _one(self, envelope: dict) -> dict:
        try:
            body = self.verify(envelope)
            if body.get("kind") != KIND:
                raise OrderRefused(f"the order is for {body.get('kind')!r}")
        except OrderRefused as exc:
            log.warning("zotero: dropped an order that does not verify: %s", exc)
            return {"status": "refused", "reason": str(exc)}
        order_id = body["order_id"]
        if order_id in self.seen:
            return {"status": "replay", "order_id": order_id}
        self.seen.add(order_id, body["not_after"])
        try:
            result = await self.run_sync({"trigger": "order", "force": bool(
                (body.get("payload") or {}).get("force"))})
            ok, note = True, _summary(result)
        except Exception as exc:  # noqa: BLE001 — the order reports the failure
            ok, note = False, f"{type(exc).__name__}: {exc}"[:300]
            log.warning("zotero: ordered sync failed: %s", note)
        try:
            await self.call("order_done", {"order_id": order_id, "ok": ok, "note": note})
        except Exception as exc:  # noqa: BLE001 — the sync ran, the report is best effort
            log.info("zotero: order_done for %s not delivered: %s", order_id, exc)
        return {"status": "ran", "order_id": order_id, "ok": ok, "note": note}

    async def run(self, retry_s: float = 30) -> None:
        """Pull forever. A return reads as a defect to the supervisor, so this never returns."""
        log.info("zotero: order puller started for node %s", self.node)
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — the approver may be down
                log.info("zotero: order pull failed, retrying in %ss: %s", retry_s, exc)
                await asyncio.sleep(retry_s)


def _summary(result: Any) -> str:
    if isinstance(result, dict):
        keys = ("status", "changed", "items", "version")
        return json.dumps({k: result[k] for k in keys if k in result})[:300]
    return str(result)[:300]


def from_env(run_sync: Callable[[dict], Awaitable[dict]]) -> Puller | None:
    settings = Settings.from_env()
    if settings is None:
        return None
    return Puller(settings.node, approver_call(settings.approver),
                  grant_verifier(settings.pubkey, settings.node), Seen(settings.seen_path),
                  run_sync)
