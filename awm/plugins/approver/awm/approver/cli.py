"""``awm-approver enrol-link``: print a one-time enrolment link, on alcor only (R85)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="awm-approver")
    sub = ap.add_subparsers(dest="cmd", required=True)
    link = sub.add_parser("enrol-link", help="print a one-time link that enrols a device")
    link.add_argument("--minutes", type=int, default=15)
    sub.add_parser("devices", help="list enrolled devices")
    args = ap.parse_args(argv)
    d = Path(os.environ.get("AWM_APPROVER_DIR", Path.home() / ".awm" / "approver"))
    req = {"cmd": args.cmd.replace("-", "_")}
    if args.cmd == "enrol-link":
        req["minutes"] = args.minutes
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(d / "control.sock"))
        s.sendall(json.dumps(req).encode() + b"\n")
        out = json.loads(s.makefile().readline())
    if isinstance(out, dict) and out.get("error"):
        print(out["error"], file=sys.stderr)
        return 1
    print(out["url"] if args.cmd == "enrol-link" else json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
