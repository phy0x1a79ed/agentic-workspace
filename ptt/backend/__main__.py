"""Run the PTT service: ``python -m ptt.backend --port 7853``."""

from __future__ import annotations

import argparse

import uvicorn

from ptt.backend.app import build_app


def main() -> None:
    p = argparse.ArgumentParser(prog="ptt.backend")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7853)
    args = p.parse_args()
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
