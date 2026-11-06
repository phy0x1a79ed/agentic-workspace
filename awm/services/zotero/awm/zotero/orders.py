"""Run a sync when Tony presses Sync now on the bibliography page (fleet.md R83).

The button lives on alcor's approver panel. A press becomes an order envelope
signed with alcor's grant key, and alcor's approver hands it to this node's
``order`` verb through deneb's singleton door. deneb opens no connection of its
own (fleet.md R79). This checks each signature with alcor's public key, runs each
order once, and starts the sync. The sync writes its outcome on the vault's status
note. Nothing here can be triggered from deneb's public front: an order that does
not verify is refused.

Orders are off until the config names alcor's public key (``ZOTERO_ORDERS_PUBKEY``).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("awm.zotero.orders")

KIND = "zotero_sync"
Verify = Callable[[dict], dict]


class OrderRefused(Exception):
    """An envelope this node will not run."""


@dataclass(frozen=True)
class Settings:
    pubkey: str
    node: str
    seen_path: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings | None:
        env = os.environ if env is None else env
        pubkey = env.get("ZOTERO_ORDERS_PUBKEY", "").strip()
        if not pubkey:
            return None
        node = env.get("AWM_NODE_NAME") or socket.gethostname().split(".")[0]
        awm_dir = Path(env.get("AWM_DIR") or Path.home() / "agentic_workspace" / ".awm")
        return cls(pubkey, node, awm_dir / "zotero" / "orders-seen.json")


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


class Orders:
    def __init__(self, verify: Verify, seen: Seen,
                 start_sync: Callable[[dict], Awaitable[None]]):
        self.verify, self.seen, self.start_sync = verify, seen, start_sync

    async def take(self, envelope: dict) -> dict:
        """Check one envelope and start its sync. Returns the order's outcome."""
        try:
            body = self.verify(envelope)
            if body.get("kind") != KIND:
                raise OrderRefused(f"the order is for {body.get('kind')!r}")
        except OrderRefused as exc:
            log.warning("zotero: refused an order that does not verify: %s", exc)
            return {"status": "refused", "reason": str(exc)}
        order_id = body["order_id"]
        if order_id in self.seen:
            return {"status": "replay", "order_id": order_id}
        self.seen.add(order_id, body["not_after"])
        await self.start_sync({"trigger": "order", "force": bool(
            (body.get("payload") or {}).get("force"))})
        return {"status": "started", "order_id": order_id}


def from_env(start_sync: Callable[[dict], Awaitable[None]]) -> Orders | None:
    settings = Settings.from_env()
    if settings is None:
        return None
    return Orders(grant_verifier(settings.pubkey, settings.node), Seen(settings.seen_path),
                  start_sync)
