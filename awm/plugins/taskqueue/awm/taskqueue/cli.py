"""``awm-taskqueue board-install``: create or update the Swarm board page in Trilium."""

from __future__ import annotations

import argparse
import json
import os

from awm.taskqueue import board
from awm.taskqueue.etapi import Etapi


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="awm-taskqueue")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("board-install", help="upsert the board page notes (reads AWM_TRILIUM_URL "
                                             "and AWM_TRILIUM_ETAPI_TOKEN)")
    b.add_argument("--approver-origin",
                   default=os.environ.get("AWM_APPROVER_ORIGIN", "https://approve.tony-xy-liu.com"))
    b.add_argument("--parent", default=os.environ.get("AWM_TRILIUM_BOARD_PARENT", "root"))
    a = p.parse_args(argv)
    url, token = os.environ.get("AWM_TRILIUM_URL"), os.environ.get("AWM_TRILIUM_ETAPI_TOKEN")
    if not url or not token:
        p.error("set AWM_TRILIUM_URL and AWM_TRILIUM_ETAPI_TOKEN")
    print(json.dumps(board.install(Etapi(url, token), a.approver_origin, a.parent)))


if __name__ == "__main__":
    main()
