"""Run the relay as a gateway service: ``python -m awm.queuerelay.hub_adapter``."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path

from verify_grant import load_public_key

from awm.gatewayclient import ServiceAdapter
from awm.queuerelay.api import API_MANIFEST, handlers
from awm.queuerelay.gateway import (APPROVER_UUID, QUEUE_STORE_UUID, REFLECTION_UUID,
                                    GatewayTool)
from awm.queuerelay.relay import Relay, order_loop


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    node = os.environ.get("AWM_NODE_NAME") or socket.gethostname()
    singleton = os.environ.get("AWM_QUEUE_PEER", "alcor")
    relay = Relay(node, Path(os.environ.get("AWM_QUEUE_RELAY_DB",
                                            Path.home() / ".awm" / "queue-relay" / "relay.db")),
                  GatewayTool(QUEUE_STORE_UUID, "queue-store", peer=singleton))
    stop = asyncio.Event()
    bg: list[asyncio.Task] = []

    async def flusher() -> None:
        while not stop.is_set():
            await relay.flush()
            await asyncio.sleep(10)

    async def on_start() -> None:
        bg.append(asyncio.create_task(flusher()))
        pub = os.environ.get("AWM_APPROVER_GRANT_PUBKEY")
        if not pub:
            logging.warning("no AWM_APPROVER_GRANT_PUBKEY: spawn orders stay off on this node")
            return
        approver = GatewayTool(APPROVER_UUID, "approver", peer=singleton, timeout=70)
        reflection = GatewayTool(REFLECTION_UUID, "reflection")
        defaults = {"project": os.environ.get("AWM_SPAWN_PROJECT", "awm"),
                    "scope": os.environ.get("AWM_SPAWN_SCOPE", "intake")}
        bg.append(asyncio.create_task(order_loop(
            relay,
            lambda: approver.call("order_pull", {"node": node, "kinds": ["spawn", "new_from"],
                                                 "wait_s": 55}),
            load_public_key(pub),
            lambda args: reflection.call("start_other", args, timeout=120),
            lambda oid, ok, note: approver.call("order_done",
                                                {"order_id": oid, "ok": ok, "note": note}),
            defaults, stop=stop)))

    await ServiceAdapter("queue-relay", API_MANIFEST, handlers(relay), on_start=on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
