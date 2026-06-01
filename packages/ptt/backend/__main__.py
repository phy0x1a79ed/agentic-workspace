"""Run the PTT backend: ``python -m backend --port <port>``.

Invoked by the hub supervisor with ``AWM_SERVICE_PORT`` in the env and
substituted into argv (``${AWM_SERVICE_PORT}``); ``--port`` reads either.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from backend.app import build_app


def main() -> None:
    default_port = int(os.environ.get("AWM_SERVICE_PORT", "7853"))
    p = argparse.ArgumentParser(prog="backend")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=default_port)
    args = p.parse_args()
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
