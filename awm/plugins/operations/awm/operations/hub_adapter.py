"""Run operations on a node: ``python -m awm.operations.hub_adapter``.

Every node runs the relay and the session verbs. The home node, alcor by default
(``AWM_OPERATIONS_HOME``), also runs the store, the approver with its panel origin, the
board writer and the singleton door's socket. Settings come from the environment and
from ``AWM_APPROVER_DIR/config.json``; see ``settings()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

from awm.operations import door
from awm.operations.api import API_MANIFEST
from awm.operations.front import Front
from awm.operations.gateway import GatewayTool, TwoFa, usage
from awm.operations.relay import Relay, Unreachable, order_loop

log = logging.getLogger("awm.operations")


def settings() -> dict[str, Any]:
    d = Path(os.environ.get("AWM_APPROVER_DIR", Path.home() / ".awm" / "approver"))
    cfg_file = Path(os.environ.get("AWM_APPROVER_CONFIG", d / "config.json"))
    cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
    return {
        "dir": d,
        "origin": os.environ.get("AWM_APPROVER_ORIGIN",
                                 cfg.get("origin", "https://auth.tony-xy-liu.com")),
        # Plain HTTP on loopback. alcor's front reads deneb's PROXY header, ends TLS
        # and sends the client address in forwarded_header (queue.md R64).
        "bind": os.environ.get("AWM_APPROVER_BIND", cfg.get("bind", "127.0.0.1:8443")),
        "board_origins": [o for o in os.environ.get(
            "AWM_APPROVER_BOARD_ORIGINS", ",".join(cfg.get("board_origins", []))).split(",") if o],
        "forwarded_header": os.environ.get("AWM_APPROVER_FORWARDED_HEADER",
                                           cfg.get("forwarded_header", "X-Forwarded-For")),
        "allowed_orders": cfg.get("allowed_orders", {"spawn": ["*"], "new_from": ["*"]}),
        "rate_max": int(cfg.get("rate_max", 30)),
        "peers": cfg.get("peers", []),
        # The pages that may ask for a passkey sign-in: {host: node} (fleet.md R84).
        "portals": cfg.get("portals", {}),
        "board_peer": os.environ.get("AWM_BOARD_PEER", cfg.get("board_peer", "deneb")),
        "board_parent": os.environ.get("AWM_TRILIUM_BOARD_PARENT", cfg.get("board_parent", "root")),
    }


def node_name() -> str:
    return (os.environ.get("AWM_NODE_NAME") or socket.gethostname()).split(".")[0].lower()


def is_home(node: str) -> bool:
    return node == os.environ.get("AWM_OPERATIONS_HOME", "alcor")


def build_home(s: dict[str, Any]):
    """The store, the approver and the writer, wired together."""
    from awm.operations.approver.core import Approver, Config
    from awm.operations.approver.keys import Keys
    from awm.operations.home import Home
    from awm.operations.store import Store
    from awm.operations.trilium import TriliumTool
    from awm.operations.writer import Writer

    store = Store(Path(os.environ.get("AWM_QUEUE_DB",
                                      Path.home() / ".awm" / "operations" / "queue.db")))
    ap = Approver(s["dir"] / "approver.db", Keys.load_or_create(s["dir"] / "keys"),
                  Config(origin=s["origin"], allowed_orders=s["allowed_orders"],
                         portals=s["portals"], rate_max=s["rate_max"]), store, TwoFa())
    writer = Writer(store, TriliumTool(peer=s["board_peer"]), parent=s["board_parent"],
                    portals=s["portals"])
    store.body_reader = writer.read_body
    return Home(store, ap, writer), writer


def operator_dm():
    """``dm(text)`` to Tony's direct message channel, or one that reports there is none."""
    from awm.config import operator_dm as target

    social = GatewayTool("social", timeout=30)

    async def dm(text: str) -> bool:
        where = target()
        if where is None:
            log.warning("no operator DM channel: set AWM_OPERATOR_DM_CHANNEL")
            return False
        try:
            out = await social.call("send", {"account": where[0], "channel": where[1], "text": text})
        except Exception as exc:  # the block is recorded either way
            log.warning("could not DM Tony: %s", exc)
            return False
        return not (isinstance(out, dict) and out.get("ok") is False)
    return dm


def handlers(front: Front, home: Any | None) -> dict[str, Any]:
    out: dict[str, Any] = {v: (lambda verb: lambda args: front.call(verb, args))(v)
                           for v in Front.VERBS}

    def operator_only(fn):
        def h(args: dict, as_: str | None = None) -> Any:
            if home is None:
                return {"ok": False, "error": "devices live on the home node (alcor)"}
            if as_ is not None or args.get("_caller_pid") or args.get("_caller_session_id"):
                return {"ok": False, "error": "an operator verb: run awm-operations on alcor"}
            return fn(args)
        return h

    async def ops_post(args: dict, as_: str | None = None) -> dict:
        """A service's notice in the Ops book. Not for sessions."""
        if as_ is not None or args.get("_caller_pid") or args.get("_caller_session_id"):
            return {"ok": False, "error": "ops_post takes this node's own services only"}
        fields = {k: args.get(k) for k in ("kind", "node", "title", "body")}
        if not all(isinstance(v, str) and v.strip() for v in fields.values()):
            return {"ok": False, "error": "kind, node, title and body are required"}
        return {"ok": True, **await front.relay.write("ops_post", fields)}

    out["ops_post"] = ops_post
    out["devices"] = operator_only(lambda a: home.approver.devices())
    out["device_revoke"] = operator_only(lambda a: home.approver.device_revoke(a["device_id"]))
    return out


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    node = node_name()
    s = settings()
    home = writer = None
    if is_home(node):
        home, writer = build_home(s)
        client: Any = door.LocalClient(home, node)
    else:
        argv = door.door_argv()
        if argv is None:
            raise SystemExit("set AWM_OPERATIONS_DOOR to alcor's singleton door, user@host:port")
        client = door.DoorClient(argv)
    relay = Relay(node, Path(os.environ.get(
        "AWM_OPERATIONS_RELAY_DB", Path.home() / ".awm" / "operations" / "relay.db")), client)
    reflection = GatewayTool("reflection")

    async def session_of(pid: int) -> dict | None:
        try:
            out = await reflection.call("session_of", {"pid": pid}, timeout=10)
        except Exception:  # noqa: BLE001 - no answer is no session
            return None
        return out if isinstance(out, dict) and out.get("ok") and out.get("session_id") else None

    async def tell(peer: str | None, job: str, text: str) -> bool:
        try:
            out = await reflection.call("message", {"job": job, "text": text}, timeout=30,
                                        peer=None if peer in (None, node) else peer)
        except Exception as exc:  # noqa: BLE001 - a missed notice never fails the call
            log.warning("could not tell %s: %s", job, exc)
            return False
        return not (isinstance(out, dict) and out.get("ok") is False)

    arbutus = None
    from awm.operations import arbutus_config
    if arbutus_config.approval_channel() is not None:
        from awm.operations.arbutus import Approvals
        arbutus = Approvals()
    front = Front(relay, node, session_of, tell, operator_dm(), arbutus)
    if arbutus is not None:
        arbutus.on_decided = front.arbutus_decided
    bg: list[asyncio.Task] = []
    stop = asyncio.Event()

    async def flusher() -> None:
        while not stop.is_set():
            await relay.flush()
            try:
                await front.remind_due()
            except Exception:  # noqa: BLE001 - a missed reminder never stops the outbox
                log.exception("urgent-block reminders failed")
            await asyncio.sleep(10)

    async def on_start() -> None:
        bg.append(asyncio.create_task(flusher()))
        if arbutus is not None:
            bg.append(asyncio.create_task(arbutus.run()))
        if home is not None:
            await start_home(home, writer, s, bg)
        pub = os.environ.get("AWM_APPROVER_GRANT_PUBKEY")
        if not pub:
            log.warning("no AWM_APPROVER_GRANT_PUBKEY: spawn orders stay off on this node")
            return
        from verify_grant import load_public_key
        defaults = {"project": os.environ.get("AWM_SPAWN_PROJECT", "awm"),
                    "scope": os.environ.get("AWM_SPAWN_SCOPE", "intake")}

        async def pull() -> list[dict]:
            return await relay.poll("order_pull", {"node": node, "kinds": ["spawn", "new_from"],
                                                   "wait_s": 55})

        bg.append(asyncio.create_task(order_loop(
            relay, pull, load_public_key(pub),
            lambda args: reflection.call("start_other", args, timeout=120),
            lambda oid, ok, note: relay.write("order_done", {"order_id": oid, "ok": ok,
                                                             "note": note}),
            defaults, stop=stop)))

    from awm.gatewayclient import ServiceAdapter
    await ServiceAdapter("operations", API_MANIFEST, handlers(front, home), on_start=on_start).run()


async def start_home(home, writer, s: dict[str, Any], bg: list[asyncio.Task]) -> None:
    import uvicorn

    from awm.operations.approver.server import create_app

    host, port = s["bind"].rsplit(":", 1)
    app = create_app(home.approver, board_origins=s["board_origins"],
                     forwarded_header=s["forwarded_header"], peers=s["peers"], usage=usage)
    cfg = uvicorn.Config(app, host=host, port=int(port), log_level="info", server_header=False,
                         proxy_headers=False)
    bg.append(asyncio.create_task(uvicorn.Server(cfg).serve()))

    async def sweep_loop() -> None:
        while True:
            try:
                await home.approver.sweep()
            except Exception:
                log.exception("sweep failed")
            await asyncio.sleep(60)

    bg.append(asyncio.create_task(sweep_loop()))
    bg.append(asyncio.create_task(writer.run_forever()))
    await door.serve(home, door.socket_path())


__all__ = ["main", "settings", "handlers", "Unreachable"]


if __name__ == "__main__":
    asyncio.run(main())
