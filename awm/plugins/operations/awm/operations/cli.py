"""``awm-operations``: the operator's commands on alcor, and the door's locked command.

- ``enrol-link`` prints a one-time link that enrols a device (R85).
- ``devices`` lists the enrolled devices.
- ``board-install`` creates or updates the Swarm board page through the trilium tool.
- ``door --node <n>`` is the command a door key is locked to (fleet.md R98 item 2).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from awm.operations.board import BOOK_ID


def _control(req: dict) -> dict:
    from awm.operations.door import socket_path

    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(socket_path()))
        s.sendall(json.dumps(req).encode() + b"\n")
        return json.loads(s.makefile().readline())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="awm-operations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    link = sub.add_parser("enrol-link", help="print a one-time link that enrols a device")
    link.add_argument("--minutes", type=int, default=15)
    sub.add_parser("devices", help="list enrolled devices")
    b = sub.add_parser("board-install", help="upsert the board page notes on deneb's vault")
    b.add_argument("--approver-origin",
                   default=os.environ.get("AWM_APPROVER_ORIGIN", "https://auth.tony-xy-liu.com"))
    b.add_argument("--parent", default=os.environ.get("AWM_TRILIUM_BOARD_PARENT", BOOK_ID))
    b.add_argument("--peer", default=os.environ.get("AWM_BOARD_PEER", "deneb"))
    d = sub.add_parser("door", help="the singleton door's locked command; reads one call on stdin")
    d.add_argument("--node", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "door":
        from awm.operations.door import forced_command
        return forced_command(args.node)
    if args.cmd == "board-install":
        from awm.operations import board
        from awm.operations.trilium import TriliumTool
        print(json.dumps(board.install(TriliumTool(peer=args.peer), args.approver_origin,
                                       args.parent)))
        return 0
    req: dict = {"cmd": args.cmd.replace("-", "_")}
    if args.cmd == "enrol-link":
        req["minutes"] = args.minutes
    out = _control(req)
    if isinstance(out, dict) and out.get("error"):
        print(out["error"], file=sys.stderr)
        return 1
    print(out["url"] if args.cmd == "enrol-link" else json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
