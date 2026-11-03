"""Run the approver: the gateway verbs, the panel origin and the local control socket.

``python -m awm.approver.hub_adapter``. Settings come from the environment; see ``settings()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import uvicorn

from awm.approver.api import API_MANIFEST, handlers
from awm.approver.core import Approver, Config
from awm.approver.keys import Keys
from awm.approver.server import create_app
from awm.approver.tools import GatewayTools

log = logging.getLogger("awm.approver")


def settings() -> dict[str, Any]:
    d = Path(os.environ.get("AWM_APPROVER_DIR", Path.home() / ".awm" / "approver"))
    cfg_file = Path(os.environ.get("AWM_APPROVER_CONFIG", d / "config.json"))
    cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
    return {
        "dir": d,
        "origin": os.environ.get("AWM_APPROVER_ORIGIN", cfg.get("origin", "https://localhost:8443")),
        "bind": os.environ.get("AWM_APPROVER_BIND", cfg.get("bind", "127.0.0.1:8443")),
        "tls_cert": os.environ.get("AWM_APPROVER_TLS_CERT", cfg.get("tls_cert")),
        "tls_key": os.environ.get("AWM_APPROVER_TLS_KEY", cfg.get("tls_key")),
        "board_origins": [o for o in os.environ.get(
            "AWM_APPROVER_BOARD_ORIGINS", ",".join(cfg.get("board_origins", []))).split(",") if o],
        "forwarded_header": os.environ.get("AWM_APPROVER_FORWARDED_HEADER",
                                           cfg.get("forwarded_header")),
        "allowed_orders": cfg.get("allowed_orders", {"spawn": ["*"], "new_from": ["*"]}),
        "rate_max": int(cfg.get("rate_max", 30)),
        "peers": cfg.get("peers", []),
        "secret": os.environ.get("AWM_QUEUE_ACTION_SECRET", "").encode(),
    }


def build(s: dict[str, Any], tools=None) -> Approver:
    if not s["secret"]:
        raise SystemExit("AWM_QUEUE_ACTION_SECRET is not set")
    return Approver(s["dir"] / "approver.db", Keys.load_or_create(s["dir"] / "keys"),
                    Config(origin=s["origin"], allowed_orders=s["allowed_orders"],
                           rate_max=s["rate_max"]),
                    tools or GatewayTools(), s["secret"])


async def control_socket(ap: Approver, path: Path) -> asyncio.AbstractServer:
    """Local commands for an operator on alcor: ``enrol_link`` (R85). The socket is 0600."""
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(await reader.readline() or b"{}")
            if req.get("cmd") == "enrol_link":
                out: Any = ap.enrol_link(int(req.get("minutes", 15)))
                out.pop("token", None)
            elif req.get("cmd") == "devices":
                out = ap.devices()
            else:
                out = {"error": "commands: enrol_link, devices"}
        except Exception as exc:  # report every failure back to the operator
            out = {"error": str(exc)}
        writer.write(json.dumps(out).encode() + b"\n")
        await writer.drain()
        writer.close()

    path.unlink(missing_ok=True)
    old = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(serve, path=str(path))
    finally:
        os.umask(old)
    return server


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = settings()
    tools = GatewayTools()
    ap = build(s, tools)
    tasks: list[asyncio.Task] = []

    async def sweep_loop() -> None:
        while True:
            try:
                await ap.sweep()
            except Exception:
                log.exception("sweep failed")
            await asyncio.sleep(60)

    async def on_start() -> None:
        host, port = s["bind"].rsplit(":", 1)
        app = create_app(ap, board_origins=s["board_origins"],
                         forwarded_header=s["forwarded_header"], peers=s["peers"],
                         usage=tools.usage)
        cfg = uvicorn.Config(app, host=host, port=int(port), log_level="info",
                             ssl_certfile=s["tls_cert"], ssl_keyfile=s["tls_key"],
                             server_header=False)
        tasks.append(asyncio.create_task(uvicorn.Server(cfg).serve()))
        tasks.append(asyncio.create_task(sweep_loop()))
        await control_socket(ap, s["dir"] / "control.sock")

    from awm.gatewayclient import ServiceAdapter
    await ServiceAdapter("approver", API_MANIFEST, handlers(ap), on_start=on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
