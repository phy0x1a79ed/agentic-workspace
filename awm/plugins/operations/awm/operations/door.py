"""alcor's singleton door for operations (fleet.md R98 item 2, R106 item 1).

A relay on another node reaches the home verbs over ssh to alcor's second port, with a
service key that alcor's ``authorized_keys`` locks to one command:

    restrict,command="awm-operations door --node <node>" ssh-ed25519 AAAA… operations@<node>

That command reads one JSON call from stdin, hands it to the operations service on a
local unix socket and prints the answer. The key names the node, so a node cannot speak
for another. The door does not depend on mizar or on the gateway.

The same socket takes the operator's commands (``awm-operations enrol-link``, R85),
but only from a process running as the service's own user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import sys
from pathlib import Path
from typing import Any

from awm.operations.home import Home
from awm.operations.relay import Refused, Unreachable

log = logging.getLogger("awm.operations.door")

MAX_LINE = 1 << 20
TIMEOUT_S = 75.0


def socket_path() -> Path:
    return Path(os.environ.get("AWM_OPERATIONS_DOOR_SOCKET",
                               Path.home() / ".awm" / "operations" / "door.sock"))


OPERATOR_COMMANDS = ("enrol_link", "devices")


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except (AttributeError, OSError):
        return None
    return struct.unpack("3i", creds)[1]


def operator_command(home: Home, req: dict[str, Any]) -> Any:
    """One local command from an operator on alcor (R85)."""
    if req.get("cmd") == "enrol_link":
        out = home.approver.enrol_link(int(req.get("minutes", 15)))
        out.pop("token", None)
        return out
    if req.get("cmd") == "devices":
        return home.approver.devices()
    return {"error": f"commands: {', '.join(OPERATOR_COMMANDS)}"}


async def serve(home: Home, path: Path) -> asyncio.AbstractServer:
    """The service side: one call per connection, ``{node, verb, args}`` in, ``{ok, …}`` out.

    A ``{cmd}`` line is an operator command, refused unless the peer runs as this user.
    """
    async def one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(await reader.readline() or b"{}")
            if "cmd" in req:
                out: Any = (operator_command(home, req) if _peer_uid(writer) == os.getuid()
                            else {"error": "operator commands run as the service's own user"})
            else:
                result = await home.call(str(req.get("verb", "")), req.get("args") or {},
                                         node=req.get("node"))
                out = {"ok": True, "result": result}
        except ValueError as exc:
            out = {"ok": False, "refused": True, "error": str(exc)}
        except Exception as exc:  # an internal fault is replayable, not a refusal
            log.exception("door call failed")
            out = {"ok": False, "error": str(exc)[:300]}
        writer.write(json.dumps(out).encode() + b"\n")
        await writer.drain()
        writer.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    old = os.umask(0o117)  # the door user reaches it through the socket's group
    try:
        return await asyncio.start_unix_server(one, path=str(path), limit=MAX_LINE)
    finally:
        os.umask(old)


def forced_command(node: str, path: Path | None = None) -> int:
    """``awm-operations door --node <node>``: the command the door's key is locked to."""
    line = sys.stdin.buffer.readline(MAX_LINE)
    try:
        req = json.loads(line or b"{}")
        verb = str(req.get("verb", ""))
    except ValueError:
        req, verb = {}, ""
    if verb not in Home.VERBS:
        print(json.dumps({"ok": False, "refused": True, "error": f"the door does not take {verb!r}"}))
        return 0
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(TIMEOUT_S)
        s.connect(str(path or socket_path()))
        s.sendall(json.dumps({"node": node, "verb": verb, "args": req.get("args") or {}}).encode()
                  + b"\n")
        sys.stdout.write(s.makefile().readline())
    return 0


def door_argv() -> list[str] | None:
    """The ssh command that reaches alcor's door, from ``AWM_OPERATIONS_DOOR`` (``user@host:port``)."""
    door = (os.environ.get("AWM_OPERATIONS_DOOR") or "").strip()
    if not door:
        return None
    target, _, port = door.rpartition(":")
    argv = ["ssh", "-T", "-p", port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=15"]
    key = os.environ.get("AWM_OPERATIONS_DOOR_KEY")
    if key:
        argv += ["-o", "IdentitiesOnly=yes", "-i", key]
    return argv + [target, "door"]


class DoorClient:
    """A relay's way to the home verbs from any node but alcor."""

    def __init__(self, argv: list[str], timeout: float = TIMEOUT_S) -> None:
        self.argv, self.timeout = argv, timeout

    async def call(self, verb: str, args: dict[str, Any]) -> Any:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps({"verb": verb, "args": args}).encode() + b"\n"),
                self.timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise Unreachable(f"door: {exc!r}") from exc
        try:
            reply = json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise Unreachable(f"door exit {proc.returncode}: {err.decode()[-200:]}") from None
        if reply.get("ok"):
            return reply.get("result")
        if reply.get("refused"):
            raise Refused(reply.get("error", "refused"))
        raise Unreachable(reply.get("error", "door fault"))


class LocalClient:
    """The home node's relay calls its own Home in-process."""

    def __init__(self, home: Home, node: str) -> None:
        self.home, self.node = home, node

    async def call(self, verb: str, args: dict[str, Any]) -> Any:
        try:
            return await self.home.call(verb, args, node=self.node)
        except ValueError as exc:
            raise Refused(str(exc)) from None
