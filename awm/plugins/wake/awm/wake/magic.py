"""Wake-on-LAN: build the magic packet, send it, and wait for the target to answer."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from typing import Any

from awm.wake.config import Target

PORT = 9
#: Sent to the subnet broadcast and the limited broadcast, as the hand script did.
LIMITED_BROADCAST = "255.255.255.255"


def packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"`{mac}` is not a MAC address")
    return b"\xff" * 6 + raw * 16


def send(target: Target, sock_factory: Callable[[], Any] | None = None) -> list[str]:
    """Send the packet to every broadcast address of ``target``. Needs no root."""
    make = sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    data = packet(target.mac)
    sent = []
    with make() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in dict.fromkeys((target.broadcast, LIMITED_BROADCAST)):
            sock.sendto(data, (addr, PORT))
            sent.append(addr)
    return sent


async def answers(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def wake(target: Target, *, wait: bool = True, budget_s: float | None = None,
               probe: Callable[[str, int], Any] = answers,
               sender: Callable[[Target], list[str]] = send,
               poll_s: float = 5.0, clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], Any] = asyncio.sleep) -> dict[str, Any]:
    """Wake ``target`` and, with ``wait``, report how long it took to answer on ssh."""
    out: dict[str, Any] = {"target": target.name, "mac": target.mac}
    if await probe(target.probe_host, target.probe_port):
        return {**out, "status": "already_up"}
    out["sent_to"] = sender(target)
    if not wait:
        return {**out, "status": "sent"}
    budget = target.wake_budget_s if budget_s is None else budget_s
    t0 = clock()
    while clock() - t0 < budget:
        await sleep(poll_s)
        if await probe(target.probe_host, target.probe_port):
            return {**out, "status": "up", "seconds": round(clock() - t0, 1)}
    return {**out, "status": "timeout", "seconds": round(clock() - t0, 1),
            "hint": f"no answer on {target.probe_host}:{target.probe_port} within "
                    f"{budget:.0f}s; check the BIOS wake setting and the NIC's Wake-on flag"}
