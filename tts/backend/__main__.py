"""Entrypoint spawned by the dev hub supervisor.

Reads ``--port`` from argv (the supervisor substitutes
``${AWM_SERVICE_PORT}`` from the env it injected). Routes are bare —
the hub strips ``<prefix>/_api`` before forwarding so the backend sees
``/healthz``, ``/engines``, ``/call/...`` directly.
"""

from __future__ import annotations

import argparse

import uvicorn

from tts.backend.app import build_app


def main() -> None:
    p = argparse.ArgumentParser(prog="tts.backend")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
