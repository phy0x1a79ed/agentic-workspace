"""Tiny echo service for smoke-testing the awm service hub.

Usage:

  # Terminal A: run the hub
  awm serve-exposed

  # Terminal B: run this service on a free port
  PATH=$AWM_ENV/bin:$PATH python -m awm.demos.echo_svc --port 7821 --prefix /demo

  # Terminal C: register the service
  awm hub register --name demo --prefix /demo --url http://127.0.0.1:7821

  # Terminal D: verify
  curl -k https://127.0.0.1:7820/demo/echo -d hello
  awm hub list
  awm hub deregister demo

Routes are mounted under the given ``--prefix`` so the same path the hub
forwards lands here unchanged.
"""

from __future__ import annotations

import argparse

import uvicorn
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi import Depends

from awm.services.auth.middleware_auth import require_peer_bearer


def build_app(prefix: str) -> FastAPI:
    app = FastAPI(title="awm-demo-echo")
    router = APIRouter(prefix=prefix)

    @router.get("/", dependencies=[Depends(require_peer_bearer)])
    async def root(req: Request):
        return {
            "service": "echo",
            "x_awm_from": req.headers.get("x-awm-from"),
            "x_awm_as": req.headers.get("x-awm-as"),
        }

    @router.post("/echo", dependencies=[Depends(require_peer_bearer)])
    async def echo(req: Request):
        body = (await req.body()).decode("utf-8", errors="replace")
        return {
            "echoed": body,
            "x_awm_from": req.headers.get("x-awm-from"),
            "x_awm_as": req.headers.get("x-awm-as"),
        }

    @router.websocket("/ws")
    async def ws(ws: WebSocket):
        # WS handshake auth: the hub presents `bearer.<local-token>` as
        # the Sec-WebSocket-Protocol; we don't validate it here for the
        # demo — production svc-* should use ``authenticate_websocket``.
        await ws.accept()
        try:
            async for msg in ws.iter_text():
                await ws.send_text(f"echo:{msg}")
        except Exception:
            return

    app.include_router(router)
    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=7821)
    p.add_argument("--prefix", default="/demo")
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()

    uvicorn.run(
        build_app(args.prefix), host=args.host, port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
